"""Route tests for the Story Readiness API."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.jev_client import JevError
from app.linear_client import LinearError, LinearIssue, LinearLabel, LinearTeam
from app.schemas import (
    CheckKind,
    CheckOut,
    ReportOut,
    SourceOut,
    StoryOut,
    StorySource,
    StoryType,
    StoryTypeChoice,
    VerdictEnum,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_ASSESS_BODY = {
    "title": "As a user I can log in",
    "description": "Some description",
    "acceptance_criteria": "Given I have an account, when I log in, then I see the dashboard",
    "definition_of_ready": [],
}

_MINIMAL_IMPORT_BODY = {"key": "PROJ-123", "definition_of_ready": []}
_LINEAR_IMPORT_BODY = {"key": "ENG-123", "definition_of_ready": []}

_MOCK_LINEAR_ISSUE = LinearIssue(
    id="uuid-123",
    identifier="ENG-123",
    title="Some Linear issue",
    description="As a user I want something",
    url="https://linear.app/eng/issue/ENG-123",
)


def _make_report(source: str = "paste", key: str | None = None, url: str | None = None) -> ReportOut:
    """Return a deterministic ReportOut for mocking."""
    return ReportOut(
        verdict=VerdictEnum.ready,
        quality=0.85,
        checks=[
            CheckOut(
                id="ac_present",
                label="Acceptance criteria present",
                kind=CheckKind.blocker,
                value=1.0,
                passed=True,
                unsure=False,
                answer={"present": 1},
                ask=[],
            ),
        ],
        story=StoryOut(
            title="As a user I can log in",
            description="Some description",
            acceptance_criteria="Given I have an account, when I log in, then I see the dashboard",
            key=key,
            source=StorySource(source),
            url=url,
        ),
        story_type=StoryType(
            choice=StoryTypeChoice.user_feature,
            confidence=0.92,
            probabilities={"user_feature": 0.92, "technical": 0.05, "bug": 0.03},
        ),
        blockers_failed=[],
        to_discuss=[],
        jev_model="jev-1",
        request_id=None,
        latency_ms=120,
        input_tokens=512,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_assess_happy_path(client, monkeypatch):
    """POST /api/assess → 200 with correct ReportOut shape."""
    monkeypatch.setattr("app.engine.assess", _async_make_report)

    resp = await client.post("/api/assess", json=_MINIMAL_ASSESS_BODY)
    assert resp.status_code == 200
    data = resp.json()
    assert "verdict" in data
    assert "quality" in data
    assert "story" in data
    assert "story_type" in data
    assert data["verdict"] == "ready"


async def test_assess_missing_title(client):
    """POST /api/assess with missing title → 422 validation error."""
    body = {k: v for k, v in _MINIMAL_ASSESS_BODY.items() if k != "title"}
    resp = await client.post("/api/assess", json=body)
    assert resp.status_code == 422


async def test_list_sources(client, monkeypatch):
    """GET /api/sources → 200, returns jira and linear entries."""
    # Ensure no env vars leak in from the test environment
    monkeypatch.delenv("JIRA_TOKEN", raising=False)
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    monkeypatch.delenv("LINEAR_TOKEN", raising=False)

    resp = await client.get("/api/sources")
    assert resp.status_code == 200
    data = resp.json()
    names = {s["name"] for s in data}
    assert names == {"jira", "linear"}
    for entry in data:
        assert entry["configured"] is False


async def test_list_sources_configured(client, monkeypatch):
    """GET /api/sources → configured=True when env vars are present."""
    monkeypatch.setenv("JIRA_TOKEN", "tok")
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")

    resp = await client.get("/api/sources")
    assert resp.status_code == 200
    data = {s["name"]: s["configured"] for s in resp.json()}
    assert data["jira"] is True
    assert data["linear"] is True


async def test_assess_from_source_jira_no_creds(client, monkeypatch):
    """POST /api/sources/jira/assess → 409 when Jira credentials are absent."""
    monkeypatch.delenv("JIRA_TOKEN", raising=False)
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)

    resp = await client.post("/api/sources/jira/assess", json=_MINIMAL_IMPORT_BODY)
    assert resp.status_code == 409


async def test_assess_from_source_linear_no_creds(client, monkeypatch):
    """POST /api/sources/linear/assess → 409 when Linear credentials are absent."""
    monkeypatch.delenv("LINEAR_TOKEN", raising=False)

    resp = await client.post("/api/sources/linear/assess", json=_MINIMAL_IMPORT_BODY)
    assert resp.status_code == 409


async def test_assess_from_source_jira_creds_present(client, monkeypatch):
    """POST /api/sources/jira/assess → 501 when credentials ARE configured."""
    monkeypatch.setenv("JIRA_TOKEN", "tok")
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")

    resp = await client.post("/api/sources/jira/assess", json=_MINIMAL_IMPORT_BODY)
    assert resp.status_code == 501


async def test_assess_from_source_linear_success(client, monkeypatch):
    """POST /api/sources/linear/assess → 200 with source=linear, key, and url from the issue."""
    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")

    async def _mock_fetch_issue(key):
        return _MOCK_LINEAR_ISSUE

    async def _mock_assess(req, source="paste", key=None, url=None, labels=None):
        return _make_report(source=source, key=key, url=url)

    monkeypatch.setattr("app.routers.assess.fetch_issue", _mock_fetch_issue)
    monkeypatch.setattr("app.engine.assess", _mock_assess)

    resp = await client.post("/api/sources/linear/assess", json=_LINEAR_IMPORT_BODY)
    assert resp.status_code == 200
    data = resp.json()
    assert data["story"]["source"] == "linear"
    assert data["story"]["key"] == "ENG-123"
    assert data["story"]["url"] == "https://linear.app/eng/issue/ENG-123"


async def test_assess_from_source_linear_not_found(client, monkeypatch):
    """POST /api/sources/linear/assess → 404 when fetch_issue raises not_found."""
    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")

    async def _mock_fetch_not_found(key):
        raise LinearError("not_found", "no such issue")

    monkeypatch.setattr("app.routers.assess.fetch_issue", _mock_fetch_not_found)

    resp = await client.post("/api/sources/linear/assess", json=_LINEAR_IMPORT_BODY)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "The issue was not found in the source."


async def test_assess_from_source_linear_upstream_error(client, monkeypatch):
    """POST /api/sources/linear/assess → 502 with sanitized detail on upstream error."""
    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")

    async def _mock_fetch_upstream(key):
        raise LinearError("upstream", "secret internal URL and tokens")

    monkeypatch.setattr("app.routers.assess.fetch_issue", _mock_fetch_upstream)

    resp = await client.post("/api/sources/linear/assess", json=_LINEAR_IMPORT_BODY)
    assert resp.status_code == 502
    body = resp.json()
    # Upstream detail must not leak
    assert "secret" not in body["detail"]
    assert len(body["detail"]) > 0


async def test_assess_from_source_linear_rejected_token_returns_409(client, monkeypatch):
    """POST /api/sources/linear/assess → 409 when Linear rejects the token (not 404/502)."""
    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")

    async def _mock_fetch_auth(key):
        raise LinearError("auth", "Authentication failed (HTTP 401)")

    monkeypatch.setattr("app.routers.assess.fetch_issue", _mock_fetch_auth)

    resp = await client.post("/api/sources/linear/assess", json=_LINEAR_IMPORT_BODY)
    assert resp.status_code == 409
    assert "rejected" in resp.json()["detail"]


async def test_assess_from_source_linear_passes_label_hints(client, monkeypatch):
    """Tracker labels reach the engine as hints; readiness:* labels do not."""
    from app.linear_client import LinearLabel

    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")
    issue = LinearIssue(
        id="uuid-123",
        identifier="ENG-123",
        title="Some Linear issue",
        description="As a user I want something",
        url="https://linear.app/eng/issue/ENG-123",
        labels=[LinearLabel(id="l1", name="Bug"), LinearLabel(id="l2", name="readiness:ready")],
    )
    seen: dict = {}

    async def _mock_fetch(key):
        return issue

    async def _mock_assess(req, source="paste", key=None, url=None, labels=None):
        seen["labels"] = labels
        return _make_report(source=source, key=key, url=url)

    monkeypatch.setattr("app.routers.assess.fetch_issue", _mock_fetch)
    monkeypatch.setattr("app.engine.assess", _mock_assess)

    resp = await client.post("/api/sources/linear/assess", json=_LINEAR_IMPORT_BODY)
    assert resp.status_code == 200
    assert seen["labels"] == ["Bug"]


async def test_assess_from_source_linear_oversized_description(client, monkeypatch):
    """POST /api/sources/linear/assess → 413 for oversized description, engine NOT called."""
    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")

    async def _mock_fetch_oversized(key):
        return LinearIssue(
            id="uuid-big",
            identifier="ENG-BIG",
            title="Big issue",
            description="x" * 10001,
            url="https://linear.app/eng/issue/ENG-BIG",
        )

    assess_called = []

    async def _mock_assess(*args, **kwargs):
        assess_called.append(True)
        return _make_report()

    monkeypatch.setattr("app.routers.assess.fetch_issue", _mock_fetch_oversized)
    monkeypatch.setattr("app.engine.assess", _mock_assess)

    resp = await client.post("/api/sources/linear/assess", json=_LINEAR_IMPORT_BODY)
    assert resp.status_code == 413
    assert resp.json()["detail"] == "The story is longer than the size limit."
    assert not assess_called


async def test_assess_from_source_linear_post_comment_posted(client, monkeypatch):
    """post_comment=true → 200 and X-Linear-Comment: posted."""
    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")

    async def _mock_fetch(key):
        return _MOCK_LINEAR_ISSUE

    async def _mock_assess(req, source="paste", key=None, url=None, labels=None):
        return _make_report(source=source, key=key, url=url)

    async def _mock_write(issue, report):
        pass  # success

    monkeypatch.setattr("app.routers.assess.fetch_issue", _mock_fetch)
    monkeypatch.setattr("app.engine.assess", _mock_assess)
    monkeypatch.setattr("app.routers.assess.write_report_comment", _mock_write)

    resp = await client.post(
        "/api/sources/linear/assess",
        json={**_LINEAR_IMPORT_BODY, "post_comment": True},
    )
    assert resp.status_code == 200
    assert resp.headers.get("x-linear-comment") == "posted"


async def test_assess_from_source_linear_comment_failure_still_200(client, monkeypatch):
    """A comment failure does not fail the request; header is 'failed'."""
    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")

    async def _mock_fetch(key):
        return _MOCK_LINEAR_ISSUE

    async def _mock_assess(req, source="paste", key=None, url=None, labels=None):
        return _make_report(source=source, key=key, url=url)

    async def _mock_write_fail(issue, report):
        raise LinearError("upstream", "network timeout")

    monkeypatch.setattr("app.routers.assess.fetch_issue", _mock_fetch)
    monkeypatch.setattr("app.engine.assess", _mock_assess)
    monkeypatch.setattr("app.routers.assess.write_report_comment", _mock_write_fail)

    resp = await client.post(
        "/api/sources/linear/assess",
        json={**_LINEAR_IMPORT_BODY, "post_comment": True},
    )
    assert resp.status_code == 200
    assert resp.headers.get("x-linear-comment") == "failed"


async def test_assess_from_source_linear_no_comment_by_default(client, monkeypatch):
    """Default request (post_comment omitted) → write_report_comment is not called."""
    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")

    async def _mock_fetch(key):
        return _MOCK_LINEAR_ISSUE

    async def _mock_assess(req, source="paste", key=None, url=None, labels=None):
        return _make_report(source=source, key=key, url=url)

    comment_called = []

    async def _mock_write(issue, report):
        comment_called.append(True)

    monkeypatch.setattr("app.routers.assess.fetch_issue", _mock_fetch)
    monkeypatch.setattr("app.engine.assess", _mock_assess)
    monkeypatch.setattr("app.routers.assess.write_report_comment", _mock_write)

    resp = await client.post("/api/sources/linear/assess", json=_LINEAR_IMPORT_BODY)
    assert resp.status_code == 200
    assert not comment_called
    assert "x-linear-comment" not in resp.headers


async def test_health(client):
    """GET /health → 200 {"status": "ok"}."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_assess_jev_error_returns_502(client, monkeypatch):
    """POST /api/assess with JevError raised → 502 with a generic (sanitized) message."""
    async def _raise_jev_error(*args, **kwargs):
        raise JevError(
            status_code=502,
            detail="POST https://api.typesafe.ai/v1/system_one: 401 Unauthorized (request_id=req_abc)",
        )

    monkeypatch.setattr("app.engine.assess", _raise_jev_error)

    resp = await client.post("/api/assess", json=_MINIMAL_ASSESS_BODY)
    assert resp.status_code == 502
    # The upstream URL and raw 401 message must NOT be exposed to the caller.
    body = resp.json()
    assert "typesafe.ai" not in body["detail"]
    assert "401" not in body["detail"]
    assert "req_abc" not in body["detail"]
    assert len(body["detail"]) > 0  # still has a meaningful message


async def test_oversized_description_returns_413(client, monkeypatch):
    """Finding 5: a description exceeding 10 000 chars must return 413, not 422."""
    monkeypatch.setattr("app.engine.assess", _async_make_report)

    body = dict(_MINIMAL_ASSESS_BODY)
    body["description"] = "x" * 10001

    resp = await client.post("/api/assess", json=body)
    assert resp.status_code == 413
    assert resp.json()["detail"] == "The story is longer than the size limit."


async def test_oversized_acceptance_criteria_returns_413(client, monkeypatch):
    """Finding 5: an acceptance_criteria field exceeding 10 000 chars must return 413, not 422."""
    monkeypatch.setattr("app.engine.assess", _async_make_report)

    body = dict(_MINIMAL_ASSESS_BODY)
    body["acceptance_criteria"] = "x" * 10001

    resp = await client.post("/api/assess", json=body)
    assert resp.status_code == 413
    assert resp.json()["detail"] == "The story is longer than the size limit."


# ---------------------------------------------------------------------------
# Async helper (must be defined at module level for monkeypatch to work)
# ---------------------------------------------------------------------------

async def _async_make_report(*args, **kwargs) -> ReportOut:
    return _make_report()


# ---------------------------------------------------------------------------
# Linear listings for the board
# ---------------------------------------------------------------------------

async def test_list_linear_teams(client, monkeypatch):
    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")

    async def _teams():
        return [LinearTeam(id="t1", key="CJD", name="Core")]

    monkeypatch.setattr("app.routers.sources.list_teams", _teams)
    resp = await client.get("/api/sources/linear/teams")
    assert resp.status_code == 200
    assert resp.json() == [{"id": "t1", "key": "CJD", "name": "Core"}]


async def test_list_linear_issues_maps_readiness_label(client, monkeypatch):
    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")
    seen = {}

    async def _issues(team_id):
        seen["team"] = team_id
        return [
            LinearIssue(id="u1", identifier="CJD-4", title="Export CSV", description="d", url="https://x/CJD-4",
                        state_type="started", labels=[LinearLabel(id="l1", name="Feature"), LinearLabel(id="l2", name="readiness:stale")]),
            LinearIssue(id="u2", identifier="CJD-7", title="New", description="", url="https://x/CJD-7"),
        ]

    monkeypatch.setattr("app.routers.sources.list_team_issues", _issues)
    resp = await client.get("/api/sources/linear/issues", params={"team": "t1"})
    assert resp.status_code == 200
    assert seen["team"] == "t1"
    first, second = resp.json()
    assert first["key"] == "CJD-4"
    assert first["labels"] == ["Feature", "readiness:stale"]
    assert first["readiness"] == "stale"
    assert first["state_type"] == "started"
    assert second["readiness"] is None


async def test_list_linear_issues_requires_team(client, monkeypatch):
    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")
    resp = await client.get("/api/sources/linear/issues")
    assert resp.status_code == 422


@pytest.mark.parametrize("path", ["/api/sources/linear/teams", "/api/sources/linear/issues?team=t1"])
async def test_linear_listings_without_token_are_409(client, monkeypatch, path):
    monkeypatch.delenv("LINEAR_TOKEN", raising=False)
    resp = await client.get(path)
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# LINEAR_ENABLED: the integration is opt-in
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "false", "0", "no"])
async def test_linear_is_off_unless_enabled(client, monkeypatch, value):
    """A token alone does not turn Linear on: /api/sources reports it as not configured."""
    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")
    if value is None:
        monkeypatch.delenv("LINEAR_ENABLED", raising=False)
    else:
        monkeypatch.setenv("LINEAR_ENABLED", value)

    resp = await client.get("/api/sources")
    assert {s["name"]: s["configured"] for s in resp.json()}["linear"] is False


@pytest.mark.parametrize("value", ["true", "TRUE", " 1 ", "yes", "on"])
async def test_linear_enabled_values(client, monkeypatch, value):
    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")
    monkeypatch.setenv("LINEAR_ENABLED", value)

    resp = await client.get("/api/sources")
    assert {s["name"]: s["configured"] for s in resp.json()}["linear"] is True


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/sources/linear/teams", None),
        ("GET", "/api/sources/linear/issues?team=t1", None),
        ("POST", "/api/sources/linear/assess", _LINEAR_IMPORT_BODY),
    ],
)
async def test_disabled_linear_endpoints_are_409_without_calling_linear(client, monkeypatch, method, path, body):
    """With LINEAR_ENABLED off, nothing reaches Linear (conftest makes any Linear call raise)."""
    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")
    monkeypatch.setenv("LINEAR_ENABLED", "false")

    resp = await client.request(method, path, json=body)
    assert resp.status_code == 409
    assert "disabled" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("kind", "status"), [("auth", 409), ("upstream", 502), ("not_found", 404)]
)
async def test_list_linear_issues_maps_linear_errors(client, monkeypatch, kind, status):
    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")

    async def _fail(team_id):
        raise LinearError(kind, "boom")

    monkeypatch.setattr("app.routers.sources.list_team_issues", _fail)
    resp = await client.get("/api/sources/linear/issues", params={"team": "t1"})
    assert resp.status_code == status
    assert resp.json()["detail"]
