"""Routes for stored stories. conftest.py gives every test a fresh SQLite file."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app import story_store
from app.jev_client import JevError
from app.main import app
from app.schemas import VerdictEnum
from tests.test_routes import _make_report

_BODY = {
    "title": "Export orders as CSV",
    "description": "As a customer I want to export my orders",
    "acceptance_criteria": "- Given 3 orders, the file has 4 lines",
    "definition_of_ready": [],
}


@pytest.fixture(autouse=True)
def board() -> story_store.Board:
    return story_store.create_board(name="Stories", key_prefix="ST")


def _stories_url() -> str:
    [board] = story_store.list_boards()
    return f"/api/boards/{board.id}/stories"


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def _mock_assess(monkeypatch, *qualities: float) -> list:
    """Make engine.assess return reports with these qualities in turn; returns the requests it saw."""
    seen = []
    remaining = list(qualities)

    async def fake(body, *, source, skip_agent_checks=False):
        seen.append((body, source, skip_agent_checks))
        report = _make_report()
        report.quality = remaining.pop(0)
        return report

    monkeypatch.setattr("app.engine.assess", fake)
    return seen


async def _create(client, **overrides) -> dict:
    resp = await client.post(_stories_url(), json={**_BODY, **overrides})
    assert resp.status_code == 201
    return resp.json()


async def test_create_returns_201_never_assessed(client):
    story = await _create(client)
    assert story["title"] == "Export orders as CSV"
    assert story["key"] == "ST-1"
    assert story["latest"] is None
    assert story["previous_quality"] is None
    assert story["stale"] is False


async def test_create_missing_title_is_422(client):
    resp = await client.post(_stories_url(), json={"description": "x"})
    assert resp.status_code == 422


async def test_create_oversized_is_413(client):
    resp = await client.post(_stories_url(), json={**_BODY, "description": "x" * 10001})
    assert resp.status_code == 413


async def test_list_shows_created_story(client):
    story = await _create(client)
    resp = await client.get(_stories_url())
    assert resp.status_code == 200
    assert [s["id"] for s in resp.json()] == [story["id"]]


async def test_unknown_story_is_404_everywhere(client):
    assert (await client.get("/api/stories/nope")).status_code == 404
    assert (await client.put("/api/stories/nope", json=_BODY)).status_code == 404
    assert (await client.delete("/api/stories/nope")).status_code == 404
    assert (await client.post("/api/stories/nope/assess")).status_code == 404


async def test_assess_saves_history_and_previous_quality(client, monkeypatch):
    seen = _mock_assess(monkeypatch, 0.58, 0.96)
    story = await _create(client, definition_of_ready=["Security impact is described"])

    first = await client.post(f"/api/stories/{story['id']}/assess")
    assert first.status_code == 200
    assert first.json()["quality"] == 0.58
    second = await client.post(f"/api/stories/{story['id']}/assess")
    assert second.json()["quality"] == 0.96

    body, source, _ = seen[0]
    assert source == "paste"
    assert body.definition_of_ready == ["Security impact is described"]

    [listed] = (await client.get(_stories_url())).json()
    assert listed["latest"]["quality"] == 0.96
    assert listed["latest"]["verdict"] == "ready"
    assert listed["previous_quality"] == 0.58

    detail = (await client.get(f"/api/stories/{story['id']}")).json()
    assert [h["quality"] for h in detail["history"]] == [0.96, 0.58]
    assert detail["report"]["quality"] == 0.96
    assert detail["report"]["model"] == "jev-1"


async def test_edit_after_assessment_marks_stale_until_reassessed(client, monkeypatch):
    _mock_assess(monkeypatch, 0.58, 0.96)
    story = await _create(client)
    await client.post(f"/api/stories/{story['id']}/assess")

    edited = await client.put(f"/api/stories/{story['id']}", json={**_BODY, "description": "Now with details"})
    assert edited.status_code == 200
    assert edited.json()["stale"] is True
    assert edited.json()["latest"]["quality"] == 0.58

    await client.post(f"/api/stories/{story['id']}/assess")
    [listed] = (await client.get(_stories_url())).json()
    assert listed["stale"] is False


async def test_delete_is_204_and_removes_story(client):
    story = await _create(client)
    resp = await client.delete(f"/api/stories/{story['id']}")
    assert resp.status_code == 204
    assert (await client.get(f"/api/stories/{story['id']}")).status_code == 404


async def test_assess_jev_error_is_502_and_saves_nothing(client, monkeypatch):
    async def failing(body, *, source, skip_agent_checks=False):
        raise JevError(503, "boom")

    monkeypatch.setattr("app.engine.assess", failing)
    story = await _create(client)
    resp = await client.post(f"/api/stories/{story['id']}/assess")
    assert resp.status_code == 502
    detail = (await client.get(f"/api/stories/{story['id']}")).json()
    assert detail["history"] == []


async def test_assess_story_deleted_during_assessment_is_404(client, monkeypatch):
    story = await _create(client)

    async def delete_then_report(body, *, source, skip_agent_checks=False):
        await client.delete(f"/api/stories/{story['id']}")
        return _make_report()

    monkeypatch.setattr("app.engine.assess", delete_then_report)
    resp = await client.post(f"/api/stories/{story['id']}/assess")
    assert resp.status_code == 404


async def test_move_changes_column_but_not_staleness(client, monkeypatch):
    _mock_assess(monkeypatch, 0.9)
    story = await _create(client)
    assert story["status"] == "backlog"
    await client.post(f"/api/stories/{story['id']}/assess")

    resp = await client.put(f"/api/stories/{story['id']}/move", json={"status": "ready_for_sprint", "position": 3})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready_for_sprint"
    assert resp.json()["position"] == 3
    assert resp.json()["stale"] is False
    assert resp.json()["latest"]["quality"] == 0.9


async def test_move_unknown_story_is_404_and_bad_status_is_422(client):
    assert (await client.put("/api/stories/nope/move", json={"status": "done", "position": 1})).status_code == 404
    story = await _create(client)
    resp = await client.put(f"/api/stories/{story['id']}/move", json={"status": "shipped", "position": 1})
    assert resp.status_code == 422


async def test_move_out_of_refinement_with_a_discuss_verdict_is_422(client, monkeypatch):
    async def discuss(body, *, source, skip_agent_checks=False):
        report = _make_report()
        report.verdict = VerdictEnum.discuss
        return report

    monkeypatch.setattr("app.engine.assess", discuss)
    story = await _create(client)
    await client.post(f"/api/stories/{story['id']}/assess")
    move = f"/api/stories/{story['id']}/move"

    resp = await client.put(move, json={"status": "ready_for_sprint", "position": 1})
    assert resp.status_code == 422
    [error] = resp.json()["detail"]
    assert (error["loc"], error["type"]) == (["body", "status"], "status_not_allowed")
    assert "Discuss" in error["msg"]
    assert (await client.put(move, json={"status": "refinement", "position": 1})).status_code == 200


async def test_move_to_blocked_keeps_reason_until_moved_out(client, monkeypatch):
    _mock_assess(monkeypatch, 0.9)
    story = await _create(client)
    await client.post(f"/api/stories/{story['id']}/assess")
    move = f"/api/stories/{story['id']}/move"

    resp = await client.put(move, json={"status": "blocked", "position": 1, "blocked_reason": "  Waiting for API keys  "})
    assert resp.status_code == 200
    assert resp.json()["blocked_reason"] == "Waiting for API keys"
    assert resp.json()["stale"] is False
    listed = (await client.get(_stories_url())).json()[0]
    assert (listed["status"], listed["blocked_reason"]) == ("blocked", "Waiting for API keys")
    detail = (await client.get(f"/api/stories/{story['id']}")).json()
    assert detail["blocked_reason"] == "Waiting for API keys"

    resp = await client.put(move, json={"status": "in_sprint", "position": 1})
    assert resp.status_code == 200
    assert resp.json()["blocked_reason"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"status": "blocked", "position": 1},
        {"status": "blocked", "position": 1, "blocked_reason": None},
        {"status": "blocked", "position": 1, "blocked_reason": "   "},
        {"status": "blocked", "position": 1, "blocked_reason": "x" * 501},
        {"status": "done", "position": 1, "blocked_reason": "Not blocked"},
    ],
)
async def test_move_blocked_reason_rules_are_422(client, body):
    story = await _create(client)
    resp = await client.put(f"/api/stories/{story['id']}/move", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]  # JSON-serialisable validation errors


# ---------------------------------------------------------------------------
# Evidence for Done
# ---------------------------------------------------------------------------

_LINK_REPORT = {"kind": "integration", "url": "https://ci.example.com/runs/7"}
_COMMIT = {"hash": "A53287D", "message": "Scaffold the monorepo"}


def _evidence(**overrides) -> dict:
    return {"test_reports": [_LINK_REPORT], "ui_change": False, "commits": [_COMMIT], **overrides}


async def _upload(client, story_id: str, name: str, data: bytes):
    return await client.post(f"/api/stories/{story_id}/evidence", files={"file": (name, data, "application/octet-stream")})


async def test_upload_move_to_done_and_download_evidence(client):
    story = await _create(client)
    resp = await _upload(client, story["id"], "report.html", b"<script>alert(1)</script>")
    assert resp.status_code == 201
    report = resp.json()
    assert (report["filename"], report["content_type"], report["size"]) == ("report.html", "text/html", 25)
    shot = (await _upload(client, story["id"], "home.png", b"\x89PNG")).json()

    move = f"/api/stories/{story['id']}/move"
    missing = await client.put(move, json={"status": "done", "position": 1})
    assert missing.status_code == 422
    assert missing.json()["detail"][0]["loc"] == ["body", "done_evidence"]

    body = _evidence(
        test_reports=[{"kind": "unit", "file_id": report["id"], "caption": "3 passed"}, _LINK_REPORT],
        ui_change=True,
        ui_evidence=[{"file_id": shot["id"]}],
    )
    resp = await client.put(move, json={"status": "done", "position": 1, "done_evidence": body})
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"

    [moved, _created] = (await client.get(f"/api/stories/{story['id']}")).json()["activity"]
    evidence = moved["detail"]["evidence"]
    assert [r["kind"] for r in evidence["test_reports"]] == ["unit", "integration"]
    assert evidence["test_reports"][0]["file"]["filename"] == "report.html"
    assert evidence["test_reports"][1] == _LINK_REPORT
    assert evidence["commits"] == [{"hash": "a53287d", "message": "Scaffold the monorepo"}]

    # An HTML report downloads and is sandboxed; a screenshot shows inline.
    html = await client.get(f"/api/evidence/{report['id']}")
    assert html.content == b"<script>alert(1)</script>"
    assert html.headers["content-disposition"].startswith("attachment")
    assert html.headers["content-security-policy"] == "sandbox"
    assert html.headers["x-content-type-options"] == "nosniff"
    png = await client.get(f"/api/evidence/{shot['id']}")
    assert (png.headers["content-type"], png.headers["content-disposition"].split(";")[0]) == ("image/png", "inline")

    # Attached evidence is kept.
    assert (await client.delete(f"/api/evidence/{report['id']}")).status_code == 409


async def test_unused_evidence_can_be_deleted(client):
    story = await _create(client)
    file = (await _upload(client, story["id"], "log.txt", b"ok")).json()
    assert (await client.delete(f"/api/evidence/{file['id']}")).status_code == 204
    assert (await client.get(f"/api/evidence/{file['id']}")).status_code == 404
    assert (await client.delete(f"/api/evidence/{file['id']}")).status_code == 404


async def test_upload_rules(client, monkeypatch):
    story = await _create(client)
    assert (await _upload(client, story["id"], "run.sh", b"echo")).status_code == 415
    assert (await _upload(client, "nope", "log.txt", b"ok")).status_code == 404
    monkeypatch.setattr(story_store, "MAX_EVIDENCE_BYTES", 4)
    assert (await _upload(client, story["id"], "log.txt", b"12345")).status_code == 413


@pytest.mark.parametrize(
    "evidence",
    [
        _evidence(test_reports=[]),
        _evidence(commits=[]),
        _evidence(ui_change=True),
        _evidence(test_reports=[{"kind": "unit"}]),
        _evidence(test_reports=[{"kind": "unit", "file_id": "x", "url": "https://ci.example.com"}]),
        _evidence(test_reports=[{"kind": "smoke", "url": "https://ci.example.com"}]),
        _evidence(test_reports=[{"kind": "unit", "url": "javascript:alert(1)"}]),
        _evidence(commits=[{"hash": "xyz1234", "message": "Nope"}]),
        _evidence(commits=[{"hash": "abc12", "message": "Too short"}]),
        _evidence(commits=[{"hash": "abc1234", "message": ""}]),
        _evidence(test_reports=[{"kind": "unit", "file_id": "not-uploaded"}]),
    ],
)
async def test_move_to_done_evidence_rules_are_422(client, evidence):
    story = await _create(client)
    resp = await client.put(f"/api/stories/{story['id']}/move", json={"status": "done", "position": 1, "done_evidence": evidence})
    assert resp.status_code == 422
    assert resp.json()["detail"]
    assert story_store.get_story(story["id"]).status == "backlog"


async def test_evidence_is_only_for_entering_done(client):
    story = await _create(client)
    move = f"/api/stories/{story['id']}/move"
    resp = await client.put(move, json={"status": "in_sprint", "position": 1, "done_evidence": _evidence()})
    assert resp.status_code == 422
    assert (await client.put(move, json={"status": "done", "position": 1, "done_evidence": _evidence()})).status_code == 200
    # Reordering within Done needs no new evidence.
    assert (await client.put(move, json={"status": "done", "position": 0.5})).status_code == 200


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------


async def test_board_changes_are_logged_as_the_board_user(client, monkeypatch):
    _mock_assess(monkeypatch, 0.8)
    story = await _create(client)
    await client.put(f"/api/stories/{story['id']}", json={**_BODY, "title": "Export orders"})
    await client.put(f"/api/stories/{story['id']}/move", json={"status": "blocked", "position": 1, "blocked_reason": "Legal"})
    await client.post(f"/api/stories/{story['id']}/assess")

    detail = (await client.get(f"/api/stories/{story['id']}")).json()
    assert [a["action"] for a in detail["activity"]] == ["story_assessed", "story_moved", "story_edited", "story_created"]
    assert {(a["actor_kind"], a["actor"], a["note"]) for a in detail["activity"]} == {("user", "board", None)}
    assert detail["activity"][1]["detail"] == {"from": "backlog", "to": "blocked", "blocked_reason": "Legal"}
    assert detail["agents"] == []


async def test_agents_show_on_list_and_detail(client):
    story = await _create(client)
    story_store.move_story(
        story["id"], status="refinement", position=1, actor=story_store.Actor("agent", "claude-code", "Ready to refine")
    )
    [listed] = (await client.get(_stories_url())).json()
    assert listed["agents"] == ["claude-code"]
    detail = (await client.get(f"/api/stories/{story['id']}")).json()
    assert detail["agents"] == ["claude-code"]
    assert detail["activity"][0]["note"] == "Ready to refine"


async def test_board_activity_endpoint(client):
    [board] = story_store.list_boards()
    await _create(client)
    await _create(client)
    url = f"/api/boards/{board.id}/activity"
    entries = (await client.get(url)).json()
    assert [(a["action"], a["story_key"]) for a in entries] == [
        ("story_created", "ST-2"), ("story_created", "ST-1"), ("board_created", None),
    ]
    assert len((await client.get(url, params={"limit": 1})).json()) == 1
    assert (await client.get(url, params={"limit": 0})).status_code == 422
    assert (await client.get(url, params={"limit": 1001})).status_code == 422
    missing = await client.get("/api/boards/nope/activity")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "The board was not found."}


# ---------------------------------------------------------------------------
# Human-only stories
# ---------------------------------------------------------------------------

async def test_new_stories_are_untagged_and_have_no_parent(client):
    story = await _create(client)
    assert (story["human_only"], story["parent_id"], story["parent_key"]) == (False, None, None)


async def test_set_and_clear_human_only_is_logged_as_the_board(client):
    story = await _create(client)
    url = f"/api/stories/{story['id']}/human-only"

    tagged = await client.put(url, json={"human_only": True})
    assert tagged.status_code == 200 and tagged.json()["human_only"] is True
    cleared = await client.put(url, json={"human_only": False})
    assert cleared.json()["human_only"] is False

    activity = (await client.get(f"/api/stories/{story['id']}")).json()["activity"]
    assert [(a["actor_kind"], a["actor"], a["action"], a["detail"]) for a in activity[:2]] == [
        ("user", "board", "story_human_only", {"human_only": False}),
        ("user", "board", "story_human_only", {"human_only": True}),
    ]
    assert (await client.put("/api/stories/nope/human-only", json={"human_only": True})).status_code == 404
    assert (await client.put(url, json={})).status_code == 422


async def test_split_child_inherits_human_only(client):
    parent = await _create(client, title="Provision staging DB creds", human_only=True)
    child = await _create(client, title="Create the DB user", parent_id=parent["id"])
    assert (child["human_only"], child["parent_id"], child["parent_key"]) == (True, parent["id"], "ST-1")

    activity = (await client.get(f"/api/stories/{child['id']}")).json()["activity"]
    assert activity[0]["detail"] == {"human_only": True, "inherited_from": "ST-1"}
    assert activity[1]["detail"] == {"title": "Create the DB user", "parent_key": "ST-1"}


async def test_split_from_a_story_on_another_board_is_422(client):
    url = _stories_url()
    other = story_store.create_board(name="Other", key_prefix="OT")
    foreign = story_store.create_story(
        other.id, title="X", description="", acceptance_criteria="", definition_of_ready=[]
    )
    resp = await client.post(url, json={**_BODY, "parent_id": foreign.id})
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "parent_id"]
    assert resp.json()["detail"][0]["type"] == "parent_invalid"


async def test_assess_skips_agent_checks_only_for_human_only_stories(client, monkeypatch):
    seen = _mock_assess(monkeypatch, 0.9, 0.9)
    untagged = await _create(client)
    tagged = await _create(client, human_only=True)
    await client.post(f"/api/stories/{untagged['id']}/assess")
    await client.post(f"/api/stories/{tagged['id']}/assess")
    assert [skip for _, _, skip in seen] == [False, True]


async def test_tagging_makes_the_latest_assessment_stale(client, monkeypatch):
    _mock_assess(monkeypatch, 0.9)
    story = await _create(client)
    await client.post(f"/api/stories/{story['id']}/assess")
    resp = await client.put(f"/api/stories/{story['id']}/human-only", json={"human_only": True})
    assert resp.json()["stale"] is True


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

async def test_tags_are_trimmed_lowercased_and_deduplicated(client):
    story = await _create(client, tags=[" Backend ", "SECURITY", "backend", "api-v2"])
    assert story["tags"] == ["backend", "security", "api-v2"]
    listed = (await client.get(_stories_url())).json()
    assert listed[0]["tags"] == ["backend", "security", "api-v2"]
    assert (await _create(client))["tags"] == []


@pytest.mark.parametrize(
    "tags",
    [["bad tag"], ["-leading-hyphen"], [""], ["x" * 31], [f"t{i}" for i in range(11)], [1]],
)
async def test_invalid_tags_are_422(client, tags):
    resp = await client.post(_stories_url(), json={**_BODY, "tags": tags})
    assert resp.status_code == 422


async def test_update_without_tags_keeps_them_and_with_tags_replaces_them(client):
    story = await _create(client, tags=["backend"])
    url = f"/api/stories/{story['id']}"
    kept = await client.put(url, json={**_BODY, "title": "Renamed"})
    assert kept.status_code == 200 and kept.json()["tags"] == ["backend"]
    replaced = await client.put(url, json={**_BODY, "title": "Renamed", "tags": ["Frontend"]})
    assert replaced.json()["tags"] == ["frontend"]
    cleared = await client.put(url, json={**_BODY, "title": "Renamed", "tags": []})
    assert cleared.json()["tags"] == []


async def test_changing_only_tags_keeps_the_verdict_current(client, monkeypatch):
    _mock_assess(monkeypatch, 0.9)
    story = await _create(client)
    assert (await client.post(f"/api/stories/{story['id']}/assess")).status_code == 200
    resp = await client.put(f"/api/stories/{story['id']}", json={**_BODY, "tags": ["platform"]})
    assert (resp.json()["tags"], resp.json()["stale"]) == (["platform"], False)
    detail = (await client.get(f"/api/stories/{story['id']}")).json()
    assert detail["activity"][0]["action"] == "story_edited"
    assert detail["activity"][0]["detail"] == {"fields": ["tags"]}
