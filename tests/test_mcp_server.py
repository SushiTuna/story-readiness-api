"""MCP tools, called in-process through mcp.Client. conftest.py gives every test a fresh SQLite file.

Each test opens its own Client: an async fixture would enter the client's task group in one task and
leave it in another, which anyio refuses.
"""
from __future__ import annotations

import pytest
from mcp import Client

from app import story_store
from app.mcp_server import _server
from tests.test_routes import _make_report


@pytest.fixture(autouse=True)
def no_agent_name_override(monkeypatch):
    """A STORY_AGENT_NAME in .env would hide the client name the tests expect."""
    monkeypatch.delenv("STORY_AGENT_NAME", raising=False)


async def _call(client, tool: str, **args):
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content[0].text
    return result.structured_content


async def _error(client, tool: str, **args) -> str:
    result = await client.call_tool(tool, args)
    assert result.is_error
    return result.content[0].text


_EVIDENCE = {
    "test_reports": [{"kind": "unit", "url": "https://ci.example.com/runs/1"}],
    "ui_change": False,
    "commits": [{"hash": "a53287d", "message": "Scaffold the monorepo"}],
}


def _mock_assess(monkeypatch, quality: float) -> None:
    async def fake(body, *, source):
        report = _make_report()
        report.quality = quality
        return report

    monkeypatch.setattr("app.engine.assess", fake)


async def test_notes_are_required_in_the_tool_schemas():
    async with Client(_server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        writes = {"create_board", "update_board", "create_story", "update_story", "move_story", "assess_story"}
        for name in writes:
            assert "note" in tools[name].input_schema["required"], name
            assert tools[name].annotations.read_only_hint is False
        for name in set(tools) - writes:
            assert tools[name].annotations.read_only_hint is True


async def test_agent_workflow_is_logged_and_tagged(monkeypatch):
    async with Client(_server) as client:
        _mock_assess(monkeypatch, 0.9)
        board = await _call(client, "create_board", name="Flowershop", key_prefix="FLW", note="New goal")
        assert board["key_prefix"] == "FLW"

        created = await _call(client, "create_story", board="FLW", title="Checkout", note="From the brief")
        assert (created["key"], created["status"], created["agents"]) == ("FLW-1", "backlog", ["mcp"])

        edited = await _call(client, "update_story", story="FLW-1", acceptance_criteria="- Pays by card", note="Add AC")
        assert (edited["title"], edited["acceptance_criteria"]) == ("Checkout", "- Pays by card")

        moved = await _call(client, "move_story", story="FLW-1", status="blocked", blocked_reason="No PSP", note="Stuck")
        assert (moved["status"], moved["blocked_reason"]) == ("blocked", "No PSP")

        assessed = await _call(client, "assess_story", story=created["id"], note="Check readiness")
        assert assessed["report"]["quality"] == 0.9
        assert [(a["action"], a["actor"], a["note"]) for a in assessed["activity"]] == [
            ("story_assessed", "mcp", "Check readiness"),
            ("story_moved", "mcp", "Stuck"),
            ("story_edited", "mcp", "Add AC"),
            ("story_created", "mcp", "From the brief"),
        ]

        [listed] = (await _call(client, "list_stories", board="FLW", status="blocked"))["result"]
        assert (listed["key"], listed["verdict"], listed["agents"]) == ("FLW-1", "ready", ["mcp"])
        assert (await _call(client, "list_stories", board="FLW", status="done"))["result"] == []

        log = (await _call(client, "get_activity", board="FLW"))["result"]
        assert log[-1]["action"] == "board_created"
        assert log[-1]["note"] == "New goal"


async def test_update_board_is_partial():
    async with Client(_server) as client:
        await _call(client, "create_board", name="Shop", key_prefix="SHP", description="Web shop", note="Start")
        board = await _call(client, "update_board", board="SHP", name="Shop 2", note="Rename")
        assert (board["name"], board["description"]) == ("Shop 2", "Web shop")


async def test_move_to_top_or_bottom_of_a_column():
    async with Client(_server) as client:
        await _call(client, "create_board", name="Shop", key_prefix="SHP", note="Start")
        for title in ("A", "B", "C"):
            await _call(client, "create_story", board="SHP", title=title, note="Add")
        top = await _call(client, "move_story", story="SHP-3", status="backlog", place="top", note="Most urgent")
        assert top["position"] == 0
        first = await _call(client, "move_story", story="SHP-1", status="done", done_evidence=_EVIDENCE, note="Shipped")
        assert first["position"] == 1


async def test_agent_name_from_env(monkeypatch):
    async with Client(_server) as client:
        monkeypatch.setenv("STORY_AGENT_NAME", "planner-bot")
        board = await _call(client, "create_board", name="Shop", key_prefix="SHP", note="Start")
        [entry] = story_store.list_activity(board["id"])
        assert (entry.actor_kind, entry.actor) == ("agent", "planner-bot")


async def test_rejected_calls_change_nothing(monkeypatch):
    async with Client(_server) as client:
        await _call(client, "create_board", name="Shop", key_prefix="SHP", note="Start")
        await _call(client, "create_story", board="SHP", title="A", note="Add")

        assert "note" in await _error(client, "create_story", board="SHP", title="B")
        assert "note is required" in await _error(client, "create_story", board="SHP", title="B", note="   ")
        assert "500 characters" in await _error(client, "create_story", board="SHP", title="B", note="x" * 501)
        assert "blocked_reason" in await _error(client, "move_story", story="SHP-1", status="blocked", note="Stuck")
        assert await _error(client, "move_story", story="SHP-1", status="doing", note="Go")
        assert await _error(client, "move_story", story="SHP-1", status="done", place="middle", note="Go")
        assert "Story not found" in await _error(client, "update_story", story="SHP-9", title="X", note="Edit")
        assert "Board not found" in await _error(client, "create_story", board="NOPE", title="X", note="Add")
        assert "size limit" in await _error(client, "create_story", board="SHP", title="X", description="x" * 10_001, note="Add")
        assert "title" in await _error(client, "update_story", story="SHP-1", title="", note="Clear")
        assert "already uses" in await _error(client, "create_board", name="Again", key_prefix="SHP", note="Dup")
        assert await _error(client, "get_activity", board="SHP", limit=0)

        monkeypatch.setattr(story_store, "MAX_STORIES_PER_BOARD", 1)
        assert "already has 1 stories" in await _error(client, "create_story", board="SHP", title="B", note="Add")

        [story] = (await _call(client, "list_stories", board="SHP"))["result"]
        assert (story["title"], story["status"]) == ("A", "backlog")
        assert [e.action for e in story_store.list_activity(story_store.get_board_by_prefix("SHP").id)] == [
            "story_created", "board_created",
        ]


async def test_activity_of_a_story_on_another_board_is_an_error():
    async with Client(_server) as client:
        await _call(client, "create_board", name="Shop", key_prefix="SHP", note="Start")
        await _call(client, "create_board", name="Ops", key_prefix="OPS", note="Start")
        await _call(client, "create_story", board="SHP", title="A", note="Add")
        assert "not on board OPS" in await _error(client, "get_activity", board="OPS", story="SHP-1")


async def test_moving_to_done_needs_evidence_and_uploads_local_files(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text("<testsuite tests='3'/>")
    shot = tmp_path / "home.png"
    shot.write_bytes(b"png")
    async with Client(_server) as client:
        await _call(client, "create_board", name="Shop", key_prefix="SHP", note="Start")
        await _call(client, "create_story", board="SHP", title="A", note="Add")

        assert "needs done_evidence" in await _error(client, "move_story", story="SHP-1", status="done", note="Done")
        assert "at least one screenshot" in await _error(
            client, "move_story", story="SHP-1", status="done", note="Done", done_evidence={**_EVIDENCE, "ui_change": True}
        )
        assert "only allowed when status is done" in await _error(
            client, "move_story", story="SHP-1", status="in_sprint", note="Go", done_evidence=_EVIDENCE
        )
        assert "must be absolute" in await _error(
            client, "move_story", story="SHP-1", status="done", note="Done",
            done_evidence={**_EVIDENCE, "test_reports": [{"kind": "unit", "path": "junit.xml"}]},
        )
        # The second file is missing: the first, already uploaded, is deleted again.
        assert "not found" in await _error(
            client, "move_story", story="SHP-1", status="done", note="Done",
            done_evidence={
                **_EVIDENCE,
                "test_reports": [{"kind": "unit", "path": str(report)}, {"kind": "cucumber", "path": str(tmp_path / "x.json")}],
            },
        )
        story = story_store.get_story_by_key("SHP-1")
        assert story.status == "backlog"
        evidence_dir = story_store._evidence_dir(story.id)
        assert not evidence_dir.exists() or not any(evidence_dir.iterdir())

        done = await _call(
            client, "move_story", story="SHP-1", status="done", note="All ACs verified",
            done_evidence={
                "test_reports": [{"kind": "unit", "path": str(report), "caption": "3 passed"}],
                "ui_change": True,
                "ui_evidence": [{"path": str(shot)}],
                "commits": [{"hash": "A53287D", "message": "Scaffold the monorepo"}],
            },
        )
        assert done["status"] == "done"
        [moved] = [e for e in story_store.list_activity(story.board_id, story_id=story.id) if e.action == "story_moved"]
        evidence = moved.detail["evidence"]
        assert evidence["test_reports"][0]["file"]["filename"] == "junit.xml"
        assert evidence["ui_evidence"][0]["file"]["content_type"] == "image/png"
        assert evidence["commits"] == [{"hash": "a53287d", "message": "Scaffold the monorepo"}]
        assert moved.note == "All ACs verified"
