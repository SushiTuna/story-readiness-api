"""Routes for boards and the stories on them. conftest.py gives every test a fresh SQLite file."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app import story_store
from app.main import app

_STORY = {"title": "Export orders as CSV", "description": "", "acceptance_criteria": "", "definition_of_ready": []}


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def _board(client, name="Flowershop Website (Frontend + Backend)", key_prefix="FLW", **extra) -> dict:
    resp = await client.post("/api/boards", json={"name": name, "key_prefix": key_prefix, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _story(client, board: dict, **overrides):
    return await client.post(f"/api/boards/{board['id']}/stories", json={**_STORY, **overrides})


async def test_create_board_is_empty_with_the_story_limit(client):
    board = await _board(client, name="  Flowershop  ", description="Frontend + backend")
    assert board["name"] == "Flowershop"
    assert board["description"] == "Frontend + backend"
    assert (board["key_prefix"], board["story_count"], board["story_limit"]) == ("FLW", 0, 100)


async def test_list_boards_oldest_first_with_story_counts(client):
    flw = await _board(client)
    ops = await _board(client, name="Platform Maintenance (AWS/Azure)", key_prefix="OPS")
    await _story(client, ops)
    listed = (await client.get("/api/boards")).json()
    assert [(b["id"], b["story_count"]) for b in listed] == [(flw["id"], 0), (ops["id"], 1)]


@pytest.mark.parametrize(
    "body",
    [
        {"name": "", "key_prefix": "FLW"},
        {"name": "   ", "key_prefix": "FLW"},
        {"name": "x" * 101, "key_prefix": "FLW"},
        {"name": "Ok", "key_prefix": "F"},
        {"name": "Ok", "key_prefix": "FLOWERS"},
        {"name": "Ok", "key_prefix": "flw"},
        {"name": "Ok", "key_prefix": "1FL"},
        {"name": "Ok", "key_prefix": "FL-W"},
        {"name": "Ok", "key_prefix": "FLW", "description": "x" * 501},
    ],
)
async def test_create_board_validation_is_422(client, body):
    assert (await client.post("/api/boards", json=body)).status_code == 422


async def test_duplicate_key_prefix_is_409(client):
    await _board(client)
    resp = await client.post("/api/boards", json={"name": "Other", "key_prefix": "FLW"})
    assert resp.status_code == 409
    assert "FLW" in resp.json()["detail"]


async def test_update_board_keeps_the_key_prefix(client):
    board = await _board(client)
    resp = await client.put(
        f"/api/boards/{board['id']}", json={"name": "Flowershop", "description": "Goal", "key_prefix": "XYZ"}
    )
    assert resp.status_code == 200
    assert (resp.json()["name"], resp.json()["description"], resp.json()["key_prefix"]) == ("Flowershop", "Goal", "FLW")


async def test_delete_board_removes_its_stories(client):
    board = await _board(client)
    story = (await _story(client, board)).json()
    assert (await client.delete(f"/api/boards/{board['id']}")).status_code == 204
    assert (await client.get("/api/boards")).json() == []
    assert (await client.get(f"/api/stories/{story['id']}")).status_code == 404


async def test_unknown_board_is_404_everywhere(client):
    assert (await client.put("/api/boards/nope", json={"name": "x"})).status_code == 404
    assert (await client.delete("/api/boards/nope")).status_code == 404
    assert (await client.get("/api/boards/nope/stories")).status_code == 404
    resp = await client.post("/api/boards/nope/stories", json=_STORY)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "The board was not found."


async def test_stories_get_their_boards_prefix_and_stay_on_their_board(client):
    flw = await _board(client)
    ops = await _board(client, name="Platform Maintenance (AWS/Azure)", key_prefix="OPS")
    f1 = (await _story(client, flw)).json()
    f2 = (await _story(client, flw)).json()
    o1 = (await _story(client, ops)).json()
    assert (f1["key"], f2["key"], o1["key"]) == ("FLW-1", "FLW-2", "OPS-1")
    assert (f1["board_id"], o1["board_id"]) == (flw["id"], ops["id"])
    assert [s["key"] for s in (await client.get(f"/api/boards/{flw['id']}/stories")).json()] == ["FLW-1", "FLW-2"]
    assert [s["key"] for s in (await client.get(f"/api/boards/{ops['id']}/stories")).json()] == ["OPS-1"]
    assert (await client.get(f"/api/stories/{o1['id']}")).json()["board_id"] == ops["id"]


async def test_full_board_refuses_the_101st_story_with_409(client):
    board = await _board(client)
    first = None
    for _ in range(story_store.MAX_STORIES_PER_BOARD):
        resp = await _story(client, board)
        assert resp.status_code == 201
        first = first or resp.json()
    # Done stories still count.
    await client.put(f"/api/stories/{first['id']}/move", json={"status": "done", "position": 1})

    resp = await _story(client, board)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "The board already has 100 stories. Delete one to make room."
    assert (await client.get("/api/boards")).json()[0]["story_count"] == 100

    other = await _board(client, name="Other", key_prefix="OPS")
    assert (await _story(client, other)).status_code == 201

    await client.delete(f"/api/stories/{first['id']}")
    assert (await _story(client, board)).status_code == 201
