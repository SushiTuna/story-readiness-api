"""Routes for stored stories. conftest.py gives every test a fresh SQLite file."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app import story_store
from app.jev_client import JevError
from app.main import app
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

    async def fake(body, *, source):
        seen.append((body, source))
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

    body, source = seen[0]
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
    async def failing(body, *, source):
        raise JevError(503, "boom")

    monkeypatch.setattr("app.engine.assess", failing)
    story = await _create(client)
    resp = await client.post(f"/api/stories/{story['id']}/assess")
    assert resp.status_code == 502
    detail = (await client.get(f"/api/stories/{story['id']}")).json()
    assert detail["history"] == []


async def test_assess_story_deleted_during_assessment_is_404(client, monkeypatch):
    story = await _create(client)

    async def delete_then_report(body, *, source):
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
