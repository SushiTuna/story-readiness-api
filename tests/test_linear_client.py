"""Tests for the Linear GraphQL client."""

from __future__ import annotations

import json
import pytest
import httpx

import app.linear_client as lc
from app.linear_client import (
    LinearError,
    LinearIssue,
    LinearLabel,
    OwnComment,
    ensure_label,
    ensure_label_group,
    fetch_issue,
    find_own_comment,
    list_team_issues,
    list_teams,
    format_report_comment,
    issue_fingerprint,
    parse_fingerprint,
    post_comment,
    update_comment,
    update_issue_labels,
    upsert_report_comment,
    write_report_comment,
)
from app.schemas import (
    CheckKind,
    CheckOut,
    ReportOut,
    StoryOut,
    StorySource,
    StoryType,
    StoryTypeChoice,
    VerdictEnum,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_transport(status_code: int, body: dict) -> httpx.MockTransport:
    """Return a MockTransport that always responds with *body* as JSON."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers={"Content-Type": "application/json"},
            content=json.dumps(body).encode(),
        )
    return httpx.MockTransport(handler)


def _error_transport(exc: Exception) -> httpx.MockTransport:
    """Return a MockTransport that always raises *exc*."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc
    return httpx.MockTransport(handler)


def _issue_body(issue: dict | None) -> dict:
    return {"data": {"issue": issue}, "errors": []}


def _comment_body(success: bool) -> dict:
    return {"data": {"commentCreate": {"success": success}}, "errors": []}


def _make_mock_client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, base_url="https://api.linear.app")


def _make_report() -> ReportOut:
    return ReportOut(
        verdict=VerdictEnum.discuss,
        quality=0.7,
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
            CheckOut(
                id="has_persona",
                label="Has persona",
                kind=CheckKind.weighted,
                value=0.4,
                passed=False,
                unsure=True,
                answer={"yes": 0.4},
                ask=["Who is the primary user?"],
            ),
        ],
        story=StoryOut(
            title="ENG-1 title",
            description="desc",
            acceptance_criteria="",
            key="ENG-1",
            source=StorySource.linear,
            url="https://linear.app/team/issue/ENG-1",
        ),
        story_type=StoryType(
            choice=StoryTypeChoice.user_feature,
            confidence=0.9,
            probabilities={"user_feature": 0.9, "technical": 0.07, "bug": 0.03},
        ),
        blockers_failed=["ac_present"],
        to_discuss=["has_persona"],
        jev_model="jev-1",
        request_id=None,
        latency_ms=100,
        input_tokens=256,
    )


# ---------------------------------------------------------------------------
# fetch_issue
# ---------------------------------------------------------------------------

async def test_fetch_issue_success(monkeypatch):
    """A well-formed response is parsed into a LinearIssue."""
    payload = _issue_body({
        "id": "uuid-1",
        "identifier": "ENG-123",
        "title": "My issue",
        "description": "Some markdown",
        "url": "https://linear.app/eng/issue/ENG-123",
    })
    monkeypatch.setenv("LINEAR_TOKEN", "my_token")
    lc._client = _make_mock_client(_json_transport(200, payload))

    issue = await fetch_issue("ENG-123")

    assert isinstance(issue, LinearIssue)
    assert issue.id == "uuid-1"
    assert issue.identifier == "ENG-123"
    assert issue.title == "My issue"
    assert issue.description == "Some markdown"
    assert issue.url == "https://linear.app/eng/issue/ENG-123"


async def test_fetch_issue_null_description_becomes_empty(monkeypatch):
    """A null `description` field is coerced to an empty string."""
    payload = _issue_body({
        "id": "uuid-2",
        "identifier": "ENG-2",
        "title": "No desc",
        "description": None,
        "url": "https://linear.app/eng/issue/ENG-2",
    })
    monkeypatch.setenv("LINEAR_TOKEN", "my_token")
    lc._client = _make_mock_client(_json_transport(200, payload))

    issue = await fetch_issue("ENG-2")
    assert issue.description == ""


async def test_fetch_issue_auth_header_has_no_bearer(monkeypatch):
    """Authorization header must NOT include the 'Bearer' prefix."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        payload = _issue_body({
            "id": "uuid-3",
            "identifier": "ENG-3",
            "title": "T",
            "description": "D",
            "url": "https://linear.app/eng/issue/ENG-3",
        })
        return httpx.Response(200, json=payload)

    monkeypatch.setenv("LINEAR_TOKEN", "lin_tok_abc")
    lc._client = _make_mock_client(httpx.MockTransport(handler))

    await fetch_issue("ENG-3")

    assert len(captured) == 1
    auth = captured[0].headers["authorization"]
    assert auth == "lin_tok_abc"
    assert "Bearer" not in auth
    assert "bearer" not in auth.lower()


async def test_fetch_issue_key_sent_as_variable(monkeypatch):
    """The issue key must be sent via GraphQL variables, not string-interpolated."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        payload = _issue_body({
            "id": "u",
            "identifier": "ENG-10",
            "title": "T",
            "description": "D",
            "url": "https://linear.app/eng/issue/ENG-10",
        })
        return httpx.Response(200, json=payload)

    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(httpx.MockTransport(handler))

    await fetch_issue("ENG-10")

    body = json.loads(captured[0].content)
    assert body["variables"] == {"id": "ENG-10"}
    # Key must NOT be embedded directly in the query string
    assert "ENG-10" not in body["query"]


async def test_fetch_issue_null_data_raises_not_found(monkeypatch):
    """A null `issue` in data → LinearError with kind 'not_found'."""
    payload = _issue_body(None)
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(_json_transport(200, payload))

    with pytest.raises(LinearError) as exc_info:
        await fetch_issue("ZZZ-999")

    assert exc_info.value.kind == "not_found"


async def test_fetch_issue_input_error_raises_not_found(monkeypatch):
    """Real Linear payload for an unknown identifier → kind 'not_found'.

    Pinned from a live call: POST /graphql with id='ZZZ-999999' returns
    code='INPUT_ERROR', userError=true, statusCode=400.
    """
    body = {
        "data": None,
        "errors": [
            {
                "message": "Entity not found: Issue",
                "path": ["issue"],
                "extensions": {
                    "type": "invalid input",
                    "code": "INPUT_ERROR",
                    "statusCode": 400,
                    "userError": True,
                    "userPresentableMessage": "Could not find referenced Issue.",
                },
            }
        ],
    }
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(_json_transport(200, body))

    with pytest.raises(LinearError) as exc_info:
        await fetch_issue("ZZZ-999999")

    assert exc_info.value.kind == "not_found"


async def test_fetch_issue_auth_error_raises_auth(monkeypatch):
    """Real Linear payload for a bad token → kind 'auth'.

    Pinned from a live call: code='AUTHENTICATION_ERROR', userError=true.
    The _graphql layer now intercepts AUTHENTICATION_ERROR and raises kind
    'auth' before fetch_issue can map it to not_found.
    """
    body = {
        "errors": [
            {
                "message": "Authentication required, not authenticated",
                "extensions": {
                    "type": "authentication error",
                    "code": "AUTHENTICATION_ERROR",
                    "statusCode": 401,
                    "userError": True,
                    "userPresentableMessage": "You need to authenticate to access this operation.",
                },
            }
        ]
    }
    monkeypatch.setenv("LINEAR_TOKEN", "bad_token")
    lc._client = _make_mock_client(_json_transport(200, body))

    with pytest.raises(LinearError) as exc_info:
        await fetch_issue("ENG-1")

    assert exc_info.value.kind == "auth"


async def test_fetch_issue_ratelimited_raises_upstream(monkeypatch):
    """HTTP 400 with RATELIMITED code → LinearError kind 'upstream'."""
    body = {
        "errors": [{"message": "Rate limited", "extensions": {"code": "RATELIMITED"}}]
    }
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(_json_transport(400, body))

    with pytest.raises(LinearError) as exc_info:
        await fetch_issue("ENG-1")

    assert exc_info.value.kind == "upstream"


async def test_fetch_issue_5xx_raises_upstream(monkeypatch):
    """HTTP 500 → LinearError kind 'upstream'."""
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(_json_transport(500, {"message": "Server error"}))

    with pytest.raises(LinearError) as exc_info:
        await fetch_issue("ENG-1")

    assert exc_info.value.kind == "upstream"


async def test_fetch_issue_transport_error_raises_upstream(monkeypatch):
    """A transport-level error → LinearError kind 'upstream'."""
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(
        _error_transport(httpx.ConnectError("connection refused"))
    )

    with pytest.raises(LinearError) as exc_info:
        await fetch_issue("ENG-1")

    assert exc_info.value.kind == "upstream"


# ---------------------------------------------------------------------------
# post_comment
# ---------------------------------------------------------------------------

async def test_post_comment_success(monkeypatch):
    """commentCreate with success=true → returns without error."""
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(_json_transport(200, _comment_body(True)))

    # Should not raise
    await post_comment("uuid-1", "## Great story")


async def test_post_comment_failure_raises_upstream(monkeypatch):
    """commentCreate with success=false → LinearError kind 'upstream'."""
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(_json_transport(200, _comment_body(False)))

    with pytest.raises(LinearError) as exc_info:
        await post_comment("uuid-1", "## body")

    assert exc_info.value.kind == "upstream"


# ---------------------------------------------------------------------------
# format_report_comment
# ---------------------------------------------------------------------------

def test_format_report_comment_contains_verdict():
    report = _make_report()
    text = format_report_comment(report)
    assert "discuss" in text


def test_format_report_comment_contains_quality_score():
    report = _make_report()
    text = format_report_comment(report)
    assert "70%" in text


def test_format_report_comment_contains_blockers():
    report = _make_report()
    text = format_report_comment(report)
    # Now shows check label, not raw id
    assert "Acceptance criteria present" in text


def test_format_report_comment_contains_to_discuss():
    report = _make_report()
    text = format_report_comment(report)
    # Now shows check label, not raw id
    assert "Has persona" in text


def test_format_report_comment_contains_ask_questions():
    report = _make_report()
    text = format_report_comment(report)
    assert "Who is the primary user?" in text


# ---------------------------------------------------------------------------
# fetch_issue — team_id and labels
# ---------------------------------------------------------------------------

async def test_fetch_issue_includes_team_and_labels(monkeypatch):
    """fetch_issue populates team_id and labels from the GraphQL response."""
    payload = {
        "data": {
            "issue": {
                "id": "uuid-lbl",
                "identifier": "ENG-50",
                "title": "Labelled issue",
                "description": "desc",
                "url": "https://linear.app/eng/issue/ENG-50",
                "team": {"id": "team-abc"},
                "labels": {
                    "nodes": [
                        {"id": "lbl-1", "name": "ready-check"},
                        {"id": "lbl-2", "name": "readiness:not_ready"},
                    ]
                },
            }
        },
        "errors": [],
    }
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(_json_transport(200, payload))

    issue = await fetch_issue("ENG-50")

    assert issue.team_id == "team-abc"
    assert len(issue.labels) == 2
    names = {l.name for l in issue.labels}
    assert names == {"ready-check", "readiness:not_ready"}


# ---------------------------------------------------------------------------
# fetch_issue — auth error (plan-pinned fixture from AUTHENTICATION_ERROR)
# ---------------------------------------------------------------------------

async def test_fetch_issue_authentication_error_raises_auth(monkeypatch):
    """AUTHENTICATION_ERROR in GraphQL errors → LinearError kind 'auth'.

    The _graphql layer now intercepts this before fetch_issue sees it.
    """
    body = {
        "errors": [
            {
                "message": "Authentication required, not authenticated",
                "extensions": {
                    "type": "authentication error",
                    "code": "AUTHENTICATION_ERROR",
                    "statusCode": 401,
                    "userError": True,
                    "userPresentableMessage": "You need to authenticate to access this operation.",
                },
            }
        ]
    }
    monkeypatch.setenv("LINEAR_TOKEN", "bad_token")
    lc._client = _make_mock_client(_json_transport(200, body))

    with pytest.raises(LinearError) as exc_info:
        await fetch_issue("ENG-1")

    assert exc_info.value.kind == "auth"


async def test_fetch_issue_http_401_raises_auth(monkeypatch):
    """HTTP 401 → LinearError kind 'auth'."""
    monkeypatch.setenv("LINEAR_TOKEN", "bad")
    lc._client = _make_mock_client(_json_transport(401, {"message": "Unauthorized"}))

    with pytest.raises(LinearError) as exc_info:
        await fetch_issue("ENG-1")

    assert exc_info.value.kind == "auth"


async def test_fetch_issue_input_error_not_entity_not_found_raises_upstream(monkeypatch):
    """INPUT_ERROR whose message does NOT start with 'Entity not found' → upstream."""
    body = {
        "data": None,
        "errors": [
            {
                "message": "Invalid input: field required",
                "extensions": {
                    "code": "INPUT_ERROR",
                    "userError": True,
                },
            }
        ],
    }
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(_json_transport(200, body))

    with pytest.raises(LinearError) as exc_info:
        await fetch_issue("ENG-1")

    assert exc_info.value.kind == "upstream"


# ---------------------------------------------------------------------------
# find_own_comment
# ---------------------------------------------------------------------------

def _sequence_transport(bodies: list[dict], captured: list[httpx.Request] | None = None) -> httpx.MockTransport:
    """Return a MockTransport that answers each request with the next body in *bodies*."""
    responses = iter(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        return httpx.Response(200, json=next(responses))
    return httpx.MockTransport(handler)


async def test_find_own_comment_filters_server_side(monkeypatch):
    """The lookup asks Linear for the viewer's comment starting with the heading."""
    captured: list[httpx.Request] = []
    body = {"data": {"issue": {"comments": {"nodes": [
        {"id": "c-1", "body": "## Story Readiness: **Ready**"},
    ]}}}}
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(_sequence_transport([body], captured))

    result = await find_own_comment("issue-id")

    assert result == OwnComment(id="c-1", body="## Story Readiness: **Ready**")
    sent = json.loads(captured[0].content)
    assert "isMe" in sent["query"]
    assert sent["variables"] == {"issueId": "issue-id", "heading": "## Story Readiness"}


async def test_find_own_comment_returns_none_when_absent(monkeypatch):
    """Returns None when the viewer has no readiness comment."""
    body = {"data": {"issue": {"comments": {"nodes": []}}}}
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(_json_transport(200, body))

    assert await find_own_comment("issue-id") is None


# ---------------------------------------------------------------------------
# upsert_report_comment / write_report_comment — create vs. update
# ---------------------------------------------------------------------------

async def test_upsert_report_comment_creates_when_none_exist(monkeypatch):
    """upsert calls commentCreate when no existing comment is found."""
    async def _no_comment(issue_id):
        return None

    created: list[tuple] = []

    async def _mock_post(issue_id, body):
        created.append((issue_id, body))
        return "new-comment-id"

    async def _should_not_update(*a):
        raise AssertionError("update_comment should not be called")

    monkeypatch.setattr(lc, "find_own_comment", _no_comment)
    monkeypatch.setattr(lc, "post_comment", _mock_post)
    monkeypatch.setattr(lc, "update_comment", _should_not_update)

    await upsert_report_comment("issue-id", "## Story Readiness: **Ready**")
    assert created == [("issue-id", "## Story Readiness: **Ready**")]


async def test_upsert_report_comment_updates_when_existing(monkeypatch):
    """upsert calls commentUpdate when an existing comment is found."""
    async def _existing_comment(issue_id):
        return OwnComment(id="existing-comment-id", body="## Story Readiness: **Not ready**")

    updated: list[tuple] = []

    async def _mock_update(comment_id, body):
        updated.append((comment_id, body))

    async def _should_not_create(*a):
        raise AssertionError("post_comment should not be called")

    monkeypatch.setattr(lc, "find_own_comment", _existing_comment)
    monkeypatch.setattr(lc, "update_comment", _mock_update)
    monkeypatch.setattr(lc, "post_comment", _should_not_create)

    await upsert_report_comment("issue-id", "## Story Readiness: **Ready**")
    assert updated == [("existing-comment-id", "## Story Readiness: **Ready**")]


async def test_write_report_comment_shows_change_and_fingerprint(monkeypatch):
    """The rewritten comment compares with the previous one and records the fingerprint."""
    issue = LinearIssue(id="issue-id", identifier="ENG-1", title="T", description="D", url="u")
    previous = (
        "## Story Readiness: **Not ready**\n\nQuality score: **14%**\n\n"
        "### Blockers failed\n\n- Acceptance criteria present\n"
    )

    async def _existing_comment(issue_id):
        return OwnComment(id="c-1", body=previous)

    updated: list[tuple] = []

    async def _mock_update(comment_id, body):
        updated.append((comment_id, body))

    monkeypatch.setattr(lc, "find_own_comment", _existing_comment)
    monkeypatch.setattr(lc, "update_comment", _mock_update)

    report = _make_report()
    report.blockers_failed = []
    report.quality = 0.94
    await write_report_comment(issue, report)

    comment_id, body = updated[0]
    assert comment_id == "c-1"
    assert "(was 14%)" in body
    assert "Verdict changed from **Not ready**" in body
    assert "### Resolved since last assessment" in body
    assert f"fingerprint: `{issue_fingerprint('T', 'D')}`" in body


# ---------------------------------------------------------------------------
# ensure_label / ensure_label_group
# ---------------------------------------------------------------------------

async def test_ensure_label_returns_existing_id(monkeypatch):
    """An existing workspace label already in the group is returned without writes."""
    body = {"data": {"issueLabels": {"nodes": [{"id": "lbl-existing", "parent": {"id": "grp"}}]}}}
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(_sequence_transport([body]))

    assert await ensure_label("readiness:ready", parent_id="grp") == "lbl-existing"


async def test_ensure_label_lookup_is_workspace_only(monkeypatch):
    """Team labels are excluded from the lookup (they can't be used on other teams' issues)."""
    captured: list[httpx.Request] = []
    body = {"data": {"issueLabels": {"nodes": [{"id": "lbl", "parent": None}]}}}
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(_sequence_transport([body], captured))

    await ensure_label("readiness:ready")

    assert "team: { null: true }" in json.loads(captured[0].content)["query"]


async def test_ensure_label_creates_in_group_when_absent(monkeypatch):
    """A missing label is created inside the given group."""
    captured: list[httpx.Request] = []
    bodies = [
        {"data": {"issueLabels": {"nodes": []}}},
        {"data": {"issueLabelCreate": {"success": True, "issueLabel": {"id": "lbl-new"}}}},
    ]
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(_sequence_transport(bodies, captured))

    assert await ensure_label("readiness:not_ready", parent_id="grp") == "lbl-new"
    assert json.loads(captured[1].content)["variables"]["input"]["parentId"] == "grp"


async def test_ensure_label_moves_existing_label_into_group(monkeypatch):
    """A label created before the group existed is moved into it."""
    captured: list[httpx.Request] = []
    bodies = [
        {"data": {"issueLabels": {"nodes": [{"id": "lbl-old", "parent": None}]}}},
        {"data": {"issueLabelUpdate": {"success": True}}},
    ]
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(_sequence_transport(bodies, captured))

    assert await ensure_label("readiness:ready", parent_id="grp") == "lbl-old"
    assert json.loads(captured[1].content)["variables"] == {"id": "lbl-old", "parentId": "grp"}


async def test_ensure_label_group_creates_group(monkeypatch):
    """A missing group is created with isGroup=true."""
    captured: list[httpx.Request] = []
    bodies = [
        {"data": {"issueLabels": {"nodes": []}}},
        {"data": {"issueLabelCreate": {"success": True, "issueLabel": {"id": "grp-new"}}}},
    ]
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(_sequence_transport(bodies, captured))

    assert await ensure_label_group("Readiness") == "grp-new"
    assert json.loads(captured[1].content)["variables"]["input"]["isGroup"] is True


# ---------------------------------------------------------------------------
# update_issue_labels
# ---------------------------------------------------------------------------

async def test_update_issue_labels_success(monkeypatch):
    """issueUpdate with addedLabelIds/removedLabelIds → success."""
    body = {"data": {"issueUpdate": {"success": True}}, "errors": []}
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(_json_transport(200, body))

    # Should not raise
    await update_issue_labels("issue-id", add=["lbl-a"], remove=["lbl-b"])


async def test_update_issue_labels_failure_raises_upstream(monkeypatch):
    """issueUpdate returning success=false → LinearError upstream."""
    body = {"data": {"issueUpdate": {"success": False}}, "errors": []}
    monkeypatch.setenv("LINEAR_TOKEN", "tok")
    lc._client = _make_mock_client(_json_transport(200, body))

    with pytest.raises(LinearError) as exc_info:
        await update_issue_labels("issue-id", add=[], remove=["lbl-b"])

    assert exc_info.value.kind == "upstream"


# ---------------------------------------------------------------------------
# format_report_comment — labels instead of ids, footer
# ---------------------------------------------------------------------------

def test_format_report_comment_uses_check_labels_for_blockers():
    """Blockers section shows check.label text, not the raw id."""
    report = _make_report()
    text = format_report_comment(report)
    # The check label is "Acceptance criteria present", not the id "ac_present"
    assert "Acceptance criteria present" in text
    assert "`ac_present`" not in text  # old backtick-id style must be gone


def test_format_report_comment_uses_check_labels_for_to_discuss():
    """To-discuss section shows check.label text."""
    report = _make_report()
    # Force has_persona into to_discuss
    report.blockers_failed = []
    report.to_discuss = ["has_persona"]
    text = format_report_comment(report)
    assert "Has persona" in text
    assert "`has_persona`" not in text


def test_format_report_comment_footer_model_and_request_id():
    """Footer includes jev_model and request_id."""
    report = _make_report()
    text = format_report_comment(report)
    assert "jev-1" in text


def test_format_report_comment_no_footer_when_no_model():
    """No footer line when jev_model is None and request_id is None."""
    report = _make_report()
    report.jev_model = None
    report.request_id = None
    text = format_report_comment(report)
    assert "---" not in text


def test_format_report_comment_readable_verdict():
    """The verdict is shown as words, not the enum value."""
    report = _make_report()
    report.verdict = "needs_refinement"
    assert "## Story Readiness: **Needs refinement**" in format_report_comment(report)


def test_format_report_comment_skips_questions_for_passed_checks():
    """A confidently passed check contributes no question."""
    report = _make_report()
    report.checks[1].passed = True
    report.checks[1].unsure = False
    assert "Who is the primary user?" not in format_report_comment(report)


def test_format_report_comment_passed_flag_adds_nothing():
    """A passed (even unsure) flag is display-only and adds no question or note."""
    report = _make_report()
    report.checks.append(CheckOut(
        id="scope_size", label="Scope / story size", kind=CheckKind.flag, value=0.6,
        passed=True, unsure=True, answer={"yes": 0.6}, ask=["Can it be split?"],
    ))
    text = format_report_comment(report)
    assert "Can it be split?" not in text


def test_format_report_comment_failed_flag_is_a_note():
    """A failed flag appears under Notes, not under Questions."""
    report = _make_report()
    report.checks[1].ask = []
    report.checks.append(CheckOut(
        id="scope_size", label="Scope / story size", kind=CheckKind.flag, value=0.2,
        passed=False, unsure=False, answer={"yes": 0.2}, ask=["Can it be split?"],
    ))
    text = format_report_comment(report)
    assert "### Notes\n\n- Can it be split?" in text
    assert "### Questions for the author" not in text


def _agent_verifiable_check(*, unsure: bool) -> CheckOut:
    return CheckOut(
        id="agent_verifiable", label="Agent: verifiable done criteria", kind=CheckKind.weighted,
        value=0.5 if unsure else 0.1, passed=False, unsure=unsure, answer={"yes": 0.1},
        ask=["How can an AI coding agent check its own work?"],
    )


def test_format_report_comment_failed_agent_check_is_a_question():
    """A confidently failed agent check asks the author, and is not a blocker."""
    report = _make_report()
    report.checks = [_agent_verifiable_check(unsure=False)]
    report.blockers_failed = []
    report.to_discuss = []
    text = format_report_comment(report)
    assert "### Questions for the author\n\n- How can an AI coding agent check its own work?" in text
    assert "### Blockers failed" not in text
    assert "### Items to discuss" not in text


def test_format_report_comment_unsure_agent_check_is_discussed_by_label():
    report = _make_report()
    report.checks = [_agent_verifiable_check(unsure=True)]
    report.blockers_failed = []
    report.to_discuss = ["agent_verifiable"]
    text = format_report_comment(report)
    assert "### Items to discuss\n\n- Agent: verifiable done criteria" in text
    assert "- How can an AI coding agent check its own work?" in text


def test_format_report_comment_no_delta_without_previous():
    """A first assessment has no 'was' or 'Resolved' parts."""
    text = format_report_comment(_make_report())
    assert "(was" not in text
    assert "Resolved since last assessment" not in text


def test_parse_fingerprint_roundtrip():
    report = _make_report()
    fp = issue_fingerprint("title", "description")
    assert parse_fingerprint(format_report_comment(report, fingerprint=fp)) == fp


def test_parse_fingerprint_missing_in_old_comment():
    assert parse_fingerprint("## Story Readiness: **not_ready**\n\nQuality score: **14%**") is None


def test_issue_fingerprint_changes_with_text():
    assert issue_fingerprint("a", "b") != issue_fingerprint("a", "c")
    assert issue_fingerprint("a", "b") == issue_fingerprint("a", "b")


# ---------------------------------------------------------------------------
# list_teams / list_team_issues
# ---------------------------------------------------------------------------

async def test_list_teams_parses_nodes(monkeypatch):
    payload = {"data": {"teams": {"nodes": [{"id": "t1", "key": "CJD", "name": "Core"}]}}}
    monkeypatch.setenv("LINEAR_TOKEN", "my_token")
    lc._client = _make_mock_client(_json_transport(200, payload))

    [team] = await list_teams()

    assert (team.id, team.key, team.name) == ("t1", "CJD", "Core")


async def test_list_team_issues_sends_filter_and_parses_issues(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        body = {"data": {"team": {"issues": {"nodes": [{
            "id": "uuid-1",
            "identifier": "CJD-4",
            "title": "Export CSV",
            "description": None,
            "url": "https://linear.app/cjd/issue/CJD-4",
            "team": {"id": "t1"},
            "labels": {"nodes": [{"id": "l1", "name": "readiness:needs_refinement"}]},
            "state": {"type": "started"},
        }]}}}}
        return httpx.Response(200, json=body)

    monkeypatch.setenv("LINEAR_TOKEN", "my_token")
    lc._client = _make_mock_client(httpx.MockTransport(handler))

    [issue] = await list_team_issues("t1", limit=25)

    assert seen["variables"] == {"id": "t1", "first": 25}
    assert 'nin: ["completed", "canceled"]' in seen["query"]
    assert issue.identifier == "CJD-4"
    assert issue.description == ""
    assert issue.team_id == "t1"
    assert issue.labels == [LinearLabel(id="l1", name="readiness:needs_refinement")]
    assert issue.state_type == "started"


async def test_list_team_issues_unknown_team_raises_not_found(monkeypatch):
    body = {"data": None, "errors": [{"message": "Entity not found: Team", "extensions": {"code": "INPUT_ERROR"}}]}
    monkeypatch.setenv("LINEAR_TOKEN", "my_token")
    lc._client = _make_mock_client(_json_transport(200, body))

    with pytest.raises(LinearError) as exc_info:
        await list_team_issues("nope")
    assert exc_info.value.kind == "not_found"


async def test_list_team_issues_other_error_raises_upstream(monkeypatch):
    body = {"data": None, "errors": [{"message": "Something broke"}]}
    monkeypatch.setenv("LINEAR_TOKEN", "my_token")
    lc._client = _make_mock_client(_json_transport(200, body))

    with pytest.raises(LinearError) as exc_info:
        await list_team_issues("t1")
    assert exc_info.value.kind == "upstream"
