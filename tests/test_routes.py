"""Route tests for the Story Readiness API."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.jev_client import JevError
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


def _make_report() -> ReportOut:
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
            key=None,
            source=StorySource.paste,
            url=None,
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


async def test_assess_from_source_linear_creds_present(client, monkeypatch):
    """POST /api/sources/linear/assess → 501 when credentials ARE configured."""
    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok")

    resp = await client.post("/api/sources/linear/assess", json=_MINIMAL_IMPORT_BODY)
    assert resp.status_code == 501


async def test_health(client):
    """GET /health → 200 {"status": "ok"}."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_assess_jev_error_returns_502(client, monkeypatch):
    """POST /api/assess with JevError raised → 502 with detail."""
    async def _raise_jev_error(*args, **kwargs):
        raise JevError(status_code=502, detail="Upstream TypeSafe error")

    monkeypatch.setattr("app.engine.assess", _raise_jev_error)

    resp = await client.post("/api/assess", json=_MINIMAL_ASSESS_BODY)
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Upstream TypeSafe error"


# ---------------------------------------------------------------------------
# Async helper (must be defined at module level for monkeypatch to work)
# ---------------------------------------------------------------------------

async def _async_make_report(*args, **kwargs) -> ReportOut:
    return _make_report()
