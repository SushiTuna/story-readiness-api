"""MCP server for the Story Board.

Agents can create, read, update, move and assess stories and boards via stdio.
Every write is logged to the activity table with the agent's name and note.

Run with: uv run --directory /abs/path/story-refinement story-board-mcp
Or register with: claude mcp add story-board -- uv run --directory /abs/path/story-refinement story-board-mcp
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from typing import Annotated, Literal

from dotenv import load_dotenv

load_dotenv()  # load .env before any SDK/config reads

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools.base import ToolAnnotations

from app import engine, story_store
from app.jev_client import JevError
from app.routers.boards import _out as _board_out
from pydantic import Field, ValidationError

from app.routers.errors import is_too_large
from app.routers.stories import _activity_out, _fields, _summary
from app.schemas import (
    ActivityOut,
    AssessRequest,
    BoardIn,
    BoardOut,
    BoardUpdateIn,
    StoredStoryDetailOut,
    StoredStoryOut,
    StoryIn,
    StoryMoveIn,
)

# Logs go to stderr only, because stdout carries the MCP stdio protocol.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger(__name__)

Status = Literal["backlog", "refinement", "ready_for_sprint", "in_sprint", "done", "blocked"]
Note = Annotated[str, Field(description="Why you are making this change (1–500 characters). Shown on the board.")]

_READ_ONLY = ToolAnnotations(read_only_hint=True)
_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
_WRITE_IDEMPOTENT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)

_INSTRUCTIONS = """
You are working with a Story Board backed by SQLite. Each board holds at most 100 stories in columns:
  backlog → refinement → ready_for_sprint → in_sprint → done (or blocked at any point).

Rules:
- A story in the "blocked" column MUST have a blocked_reason.
- Stories have a human-readable key like FLW-12. You can use keys or IDs in all tools.
- Boards are identified by ID or key prefix (e.g. "FLW").

IMPORTANT – every change you make is logged under your agent name with your note, and is visible
on the board and in the activity side panel. Write clear, concise notes that explain *why* you are
making each change. Notes are required on all write operations.

The agent identity used for audit is:
  1. STORY_AGENT_NAME env var (if set), or
  2. The MCP client's reported name, or
  3. "mcp-agent"
""".strip()

_server = MCPServer("story-board", instructions=_INSTRUCTIONS)


def _agent_name(ctx: Context) -> str:
    """Resolve the agent name: env override → client info → fallback."""
    env = os.getenv("STORY_AGENT_NAME", "").strip()
    if env:
        return env
    try:
        params = ctx.session.client_params
        if params is not None:
            return params.client_info.name
    except Exception:
        pass
    return "mcp-agent"


def _actor(ctx: Context, note: str) -> story_store.Actor:
    return story_store.Actor("agent", _agent_name(ctx), note)


def _resolve_board(board_ref: str) -> story_store.Board:
    """Resolve a board by ID or key prefix. Raises ToolError if not found."""
    b = story_store.get_board(board_ref)
    if b is None:
        b = story_store.get_board_by_prefix(board_ref)
    if b is None:
        raise ToolError(f"Board not found: {board_ref!r}. Use a board ID or key prefix like 'FLW'.")
    return b


def _resolve_story(story_ref: str) -> story_store.StoredStory:
    """Resolve a story by ID or human key (e.g. FLW-12). Raises ToolError if not found."""
    s = story_store.get_story(story_ref)
    if s is None:
        s = story_store.get_story_by_key(story_ref)
    if s is None:
        raise ToolError(f"Story not found: {story_ref!r}. Use a story ID or key like 'FLW-12'.")
    return s


def _require_note(note: str | None) -> str:
    if not note or not note.strip():
        raise ToolError("note is required. Explain why you are making this change (1–500 characters).")
    stripped = note.strip()
    if len(stripped) > 500:
        raise ToolError("note must be 500 characters or fewer.")
    return stripped


def _invalid(exc: ValidationError) -> ToolError:
    """A ToolError listing each validation message, without pydantic's input dump and links."""
    messages = []
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"])
        messages.append(f"{field}: {error['msg']}" if field else error["msg"])
    return ToolError("; ".join(messages))


def _agents(story: story_store.StoredStory) -> list[str]:
    return story_store.agents_by_story(story.board_id).get(story.id, [])


def _out(story: story_store.StoredStory) -> StoredStoryOut:
    recent = story_store.list_assessments(story.id)[:2]
    return StoredStoryOut(**_fields(story, recent, _agents(story)))


def _detail(story: story_store.StoredStory) -> StoredStoryDetailOut:
    history = story_store.list_assessments(story.id)
    activity = story_store.list_activity(story.board_id, story_id=story.id)
    return StoredStoryDetailOut(
        **_fields(story, history, _agents(story)),
        report=history[0].report() if history else None,
        history=[_summary(a) for a in history],
        activity=[_activity_out(e) for e in activity],
    )


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------

@_server.tool(annotations=_READ_ONLY, description="List all boards with their story counts and story limit.")
def list_boards() -> list[BoardOut]:
    return [_board_out(b) for b in story_store.list_boards()]


@_server.tool(
    annotations=_READ_ONLY,
    description=(
        "List stories on a board. Returns a compact view: key, title, status, verdict, quality, "
        "stale, blocked_reason, agents. "
        "board: board ID or key prefix (e.g. 'FLW'). "
        "status: optional filter, one of backlog/refinement/ready_for_sprint/in_sprint/done/blocked."
    ),
)
def list_stories(board: str, status: Status | None = None) -> list[dict]:
    b = _resolve_board(board)
    pairs = story_store.list_stories(b.id)
    agents_map = story_store.agents_by_story(b.id)
    result = []
    for story, recent in pairs:
        if status is not None and story.status != status:
            continue
        latest = recent[0] if recent else None
        result.append({
            "key": story.key,
            "id": story.id,
            "title": story.title,
            "status": story.status,
            "verdict": latest.verdict if latest else None,
            "quality": latest.quality if latest else None,
            "stale": latest is not None and latest.fingerprint != story.fingerprint,
            "blocked_reason": story.blocked_reason,
            "agents": agents_map.get(story.id, []),
        })
    return result


@_server.tool(
    annotations=_READ_ONLY,
    description=(
        "Get the full details of a story: text, latest assessment report (checks and questions for "
        "the author), assessment history, and activity log. "
        "story: story ID or key (e.g. 'FLW-12')."
    ),
)
def get_story(story: str) -> StoredStoryDetailOut:
    return _detail(_resolve_story(story))


@_server.tool(
    annotations=_READ_ONLY,
    description=(
        "Get the activity log for a board or a specific story, newest first. "
        "board: board ID or key prefix. "
        "story: optional story ID or key to filter to one story. "
        "limit: max entries to return (default 50)."
    ),
)
def get_activity(
    board: str, story: str | None = None, limit: Annotated[int, Field(ge=1, le=1000)] = 50
) -> list[ActivityOut]:
    b = _resolve_board(board)
    story_id: str | None = None
    if story is not None:
        s = _resolve_story(story)
        if s.board_id != b.id:
            raise ToolError(f"Story {s.key} is not on board {b.key_prefix}.")
        story_id = s.id
    entries = story_store.list_activity(b.id, story_id=story_id, limit=limit)
    return [_activity_out(e) for e in entries]


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------

@_server.tool(
    annotations=_WRITE,
    description=(
        "Create a new board. "
        "name: display name (1–100 chars). "
        "key_prefix: uppercase letters/digits starting with a letter, 2–6 chars (e.g. FLW). Unique. "
        "description: optional board description (max 500 chars). "
        "note: required — why you are creating this board."
    ),
)
def create_board(name: str, key_prefix: str, note: Note, ctx: Context, description: str = "") -> BoardOut:
    note = _require_note(note)
    try:
        body = BoardIn(name=name, key_prefix=key_prefix, description=description)
    except ValidationError as exc:
        raise _invalid(exc) from exc
    try:
        b = story_store.create_board(
            name=body.name,
            key_prefix=body.key_prefix,
            description=body.description,
            actor=_actor(ctx, note),
        )
    except story_store.DuplicateKeyPrefixError:
        raise ToolError(f"Another board already uses the key prefix {key_prefix!r}.")
    return _board_out(b)


@_server.tool(
    annotations=_WRITE_IDEMPOTENT,
    description=(
        "Update a board's name or description (partial — omit a field to keep its current value). "
        "board: board ID or key prefix. "
        "note: required — why you are making this change."
    ),
)
def update_board(
    board: str,
    note: Note,
    ctx: Context,
    name: str | None = None,
    description: str | None = None,
) -> BoardOut:
    note = _require_note(note)
    b = _resolve_board(board)
    merged_name = name if name is not None else b.name
    merged_desc = description if description is not None else b.description
    try:
        body = BoardUpdateIn(name=merged_name, description=merged_desc)
    except ValidationError as exc:
        raise _invalid(exc) from exc
    updated = story_store.update_board(b.id, name=body.name, description=body.description, actor=_actor(ctx, note))
    if updated is None:
        raise ToolError(f"Board not found: {board!r}.")
    return _board_out(updated)


@_server.tool(
    annotations=_WRITE,
    description=(
        "Create a story on a board. "
        "board: board ID or key prefix. "
        "title: story title (1–500 chars). "
        "description: story body (max 10 000 chars). "
        "acceptance_criteria: acceptance criteria (max 10 000 chars). "
        "definition_of_ready: list of DoR items (max 50). "
        "note: required — why you are creating this story."
    ),
)
def create_story(
    board: str,
    title: str,
    note: Note,
    ctx: Context,
    description: str = "",
    acceptance_criteria: str = "",
    definition_of_ready: list[str] | None = None,
) -> StoredStoryOut:
    note = _require_note(note)
    dor = definition_of_ready or []
    if is_too_large(description, acceptance_criteria):
        raise ToolError("The story is longer than the size limit (10 000 chars per field).")
    try:
        body = StoryIn(
            title=title,
            description=description,
            acceptance_criteria=acceptance_criteria,
            definition_of_ready=dor,
        )
    except ValidationError as exc:
        raise _invalid(exc) from exc
    b = _resolve_board(board)
    try:
        s = story_store.create_story(
            b.id,
            title=body.title,
            description=body.description,
            acceptance_criteria=body.acceptance_criteria,
            definition_of_ready=body.definition_of_ready,
            actor=_actor(ctx, note),
        )
    except story_store.BoardNotFoundError:
        raise ToolError(f"Board not found: {board!r}.")
    except story_store.BoardFullError:
        raise ToolError(
            f"The board already has {story_store.MAX_STORIES_PER_BOARD} stories. Delete one to make room."
        )
    return _out(s)


@_server.tool(
    annotations=_WRITE_IDEMPOTENT,
    description=(
        "Update a story's text (partial — omit a field to keep its current value). "
        "Marks the story stale until it is re-assessed. "
        "story: story ID or key (e.g. 'FLW-12'). "
        "note: required — why you are making this change."
    ),
)
def update_story(
    story: str,
    note: Note,
    ctx: Context,
    title: str | None = None,
    description: str | None = None,
    acceptance_criteria: str | None = None,
    definition_of_ready: list[str] | None = None,
) -> StoredStoryOut:
    note = _require_note(note)
    s = _resolve_story(story)
    merged_title = title if title is not None else s.title
    merged_desc = description if description is not None else s.description
    merged_ac = acceptance_criteria if acceptance_criteria is not None else s.acceptance_criteria
    merged_dor = definition_of_ready if definition_of_ready is not None else s.definition_of_ready
    if is_too_large(merged_desc, merged_ac):
        raise ToolError("The story is longer than the size limit (10 000 chars per field).")
    try:
        body = StoryIn(
            title=merged_title,
            description=merged_desc,
            acceptance_criteria=merged_ac,
            definition_of_ready=merged_dor,
        )
    except ValidationError as exc:
        raise _invalid(exc) from exc
    updated = story_store.update_story(
        s.id,
        title=body.title,
        description=body.description,
        acceptance_criteria=body.acceptance_criteria,
        definition_of_ready=body.definition_of_ready,
        actor=_actor(ctx, note),
    )
    if updated is None:
        raise ToolError(f"Story not found: {story!r}.")
    return _out(updated)


@_server.tool(
    annotations=_WRITE,
    description=(
        "Move a story to a different workflow column and position. "
        "story: story ID or key (e.g. 'FLW-12'). "
        "status: one of backlog/refinement/ready_for_sprint/in_sprint/done/blocked. "
        "place: 'top' (position = min - 1) or 'bottom' (position = max + 1, default). "
        "blocked_reason: required when moving to 'blocked'; not allowed otherwise. "
        "note: required — why you are moving this story."
    ),
)
def move_story(
    story: str,
    status: Status,
    note: Note,
    ctx: Context,
    place: Literal["top", "bottom"] = "bottom",
    blocked_reason: str | None = None,
) -> StoredStoryOut:
    note = _require_note(note)
    s = _resolve_story(story)
    # Validate via Pydantic so the Blocked rule fires.
    try:
        body = StoryMoveIn(
            status=status,
            position=0,  # placeholder; we compute the real one below
            blocked_reason=blocked_reason,
        )
    except ValidationError as exc:
        raise _invalid(exc) from exc
    # Same rule as positionBetween() in the board UI: one before the first card, or one after the last.
    bounds = story_store.column_bounds(s.board_id, body.status)
    if bounds is None:
        position = 1.0
    else:
        position = bounds[0] - 1 if place == "top" else bounds[1] + 1
    updated = story_store.move_story(
        s.id,
        status=body.status,
        position=position,
        blocked_reason=body.blocked_reason,
        actor=_actor(ctx, note),
    )
    if updated is None:
        raise ToolError(f"Story not found: {story!r}.")
    return _out(updated)


@_server.tool(
    annotations=_WRITE,
    description=(
        "Assess a story: runs the TypeSafe/Jev model and saves the report. "
        "story: story ID or key (e.g. 'FLW-12'). "
        "note: required — why you are assessing this story (e.g. 'Checking readiness before sprint planning')."
    ),
)
async def assess_story(story: str, note: Note, ctx: Context) -> StoredStoryDetailOut:
    note = _require_note(note)
    s = _resolve_story(story)
    if is_too_large(s.description, s.acceptance_criteria):
        raise ToolError("The story is longer than the size limit (10 000 chars per field).")
    request = AssessRequest(
        title=s.title,
        description=s.description,
        acceptance_criteria=s.acceptance_criteria,
        definition_of_ready=s.definition_of_ready,
    )
    try:
        report = await engine.assess(request, source="paste")
    except JevError as exc:
        raise ToolError(f"Assessment service unavailable: {exc.detail}") from exc
    try:
        story_store.add_assessment(s, report, actor=_actor(ctx, note))
    except sqlite3.IntegrityError as exc:
        raise ToolError("Story was deleted during assessment.") from exc
    fresh = story_store.get_story(s.id)
    if fresh is None:
        raise ToolError("Story was deleted during assessment.")
    return _detail(fresh)


def main() -> None:
    story_store.init_db()
    _server.run("stdio")


if __name__ == "__main__":
    main()
