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
from pathlib import Path
from typing import Annotated, Literal

from dotenv import load_dotenv

load_dotenv()  # load .env before any SDK/config reads

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools.base import ToolAnnotations

from app import engine, story_store
from app.jev_client import JevError
from app.routers.boards import _out as _board_out
from pydantic import BaseModel, Field, ValidationError

from app.routers.errors import is_too_large
from app.routers.stories import _activity_out, _fields, _summary
from app.schemas import (
    ActivityOut,
    AssessRequest,
    BoardIn,
    BoardOut,
    BoardUpdateIn,
    CommitIn,
    DoneEvidenceIn,
    CompactCheckOut,
    CompactReportOut,
    ReportOut,
    StoredStoryOut,
    StoryDetailMcpOut,
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
- While a story's latest verdict is "discuss" or "needs_refinement" (even if stale), it can only move to
  "backlog" or "refinement", or within its current column. Refine it and assess it again to lift this.
- Moving a story INTO "done" MUST include done_evidence: at least one test report (unit, integration or
  cucumber) as a local file path or a URL, ui_change (true if the story changes the UI; then at least one
  screenshot or recording), and at least one commit (hash and message). The evidence is attached to the
  story's activity log. Do not move a story to done without real evidence.
- A story with human_only: true depends on a person: accounts, servers, another team's API, or secrets and
  private keys. You can read it, but you can't edit, move, assess or split it, and you can't set or clear the
  tag. Those calls are rejected. Leave it to the people on the board.
- Stories can have up to 10 tags for the kind of work, e.g. backend, frontend, design, platform, security.
  Tags are lowercase letters, digits and hyphens. Reuse the tags already on the board where they fit.
  Changing only the tags doesn't make the assessment stale.
- A story can wait on other stories on its board (blocked_by). It can't move INTO "in_sprint" or "done"
  until every story it waits on is done; it can still move anywhere else. Pick up the stories it waits on
  first. Use add_blocker when one story needs another finished first, and remove_blocker when it no longer does.
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


def _refuse_human_only(story: story_store.StoredStory) -> None:
    """Reject any agent change to a human-only story before doing work (uploads, Jev calls).

    The store checks again inside its write transaction, so a tag set in the meantime still holds.
    """
    if story.human_only:
        raise ToolError(story_store.human_only_message(story.key))


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


def _compact_report(report: ReportOut, full: bool) -> CompactReportOut:
    checks = report.checks if full else [
        CompactCheckOut(id=c.id, label=c.label, passed=c.passed, unsure=c.unsure, ask=c.ask)
        for c in report.checks
        if not c.passed or c.unsure
    ]
    return CompactReportOut(
        verdict=report.verdict,
        quality=report.quality,
        story_type=report.story_type,
        blockers_failed=report.blockers_failed,
        to_discuss=report.to_discuss,
        agent_checks_skipped=report.agent_checks_skipped,
        checks=checks,
    )


def _detail(
    story: story_store.StoredStory, history_limit: int = 5, activity_limit: int = 10, full_report: bool = False
) -> StoryDetailMcpOut:
    history = story_store.list_assessments(story.id)
    # One extra entry tells us whether older activity was left out.
    activity = story_store.list_activity(story.board_id, story_id=story.id, limit=activity_limit + 1)
    report = history[0].report() if history else None
    return StoryDetailMcpOut(
        **_fields(story, history, _agents(story)),
        report=_compact_report(report, full_report) if report else None,
        history=[_summary(a) for a in history[:history_limit]],
        history_total=len(history),
        activity=[_activity_out(e) for e in activity[:activity_limit]],
        activity_more=len(activity) > activity_limit,
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
        "stale, blocked_reason, agents, human_only, parent_key, tags, blocked_by (keys of stories it waits on that "
        "are not done yet) and blocks (keys of stories waiting on it). Human-only stories can be read but not changed. "
        "board: board ID or key prefix (e.g. 'FLW'). "
        "status: optional filter, one of backlog/refinement/ready_for_sprint/in_sprint/done/blocked. "
        "tag: optional filter, only stories with this tag (e.g. 'backend')."
    ),
)
def list_stories(board: str, status: Status | None = None, tag: str | None = None) -> list[dict]:
    b = _resolve_board(board)
    pairs = story_store.list_stories(b.id)
    agents_map = story_store.agents_by_story(b.id)
    wanted_tag = tag.strip().lower() if tag is not None else None
    result = []
    for story, recent in pairs:
        if status is not None and story.status != status:
            continue
        if wanted_tag is not None and wanted_tag not in story.tags:
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
            "human_only": story.human_only,
            "parent_key": story.parent_key,
            "tags": story.tags,
            "blocked_by": [r.key for r in story.blocked_by if r.status != "done"],
            "blocks": [r.key for r in story.blocks],
        })
    return result


@_server.tool(
    annotations=_READ_ONLY,
    description=(
        "Get a story: text, latest assessment report (failed or unsure checks with questions for the author), "
        "recent assessment history and recent activity. "
        "story: story ID or key (e.g. 'FLW-12'). "
        "history_limit: max assessments to return (default 5); history_total gives the full count. "
        "activity_limit: max activity entries to return (default 10); use get_activity for the full log. "
        "full_report: true to return every check in full, including passed ones."
    ),
)
def get_story(
    story: str,
    history_limit: Annotated[int, Field(ge=0, le=100)] = 5,
    activity_limit: Annotated[int, Field(ge=0, le=100)] = 10,
    full_report: bool = False,
) -> StoryDetailMcpOut:
    return _detail(_resolve_story(story), history_limit, activity_limit, full_report)


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
        "parent: optional story ID or key on the same board to split this story from. A human-only story can't "
        "be split by an agent. "
        "tags: optional list of up to 10 tags for the kind of work, e.g. ['backend', 'security']; lowercase "
        "letters, digits and hyphens. "
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
    parent: str | None = None,
    tags: list[str] | None = None,
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
            tags=tags or [],
        )
    except ValidationError as exc:
        raise _invalid(exc) from exc
    b = _resolve_board(board)
    parent_id: str | None = None
    if parent is not None:
        p = _resolve_story(parent)
        if p.board_id != b.id:
            raise ToolError(f"Story {p.key} is not on board {b.key_prefix}; split a story on the same board.")
        if p.human_only:
            raise ToolError(
                f"{story_store.human_only_message(p.key)} Stories split from it are human-only too, so only people "
                "on the board can create them."
            )
        parent_id = p.id
    try:
        s = story_store.create_story(
            b.id,
            title=body.title,
            description=body.description,
            acceptance_criteria=body.acceptance_criteria,
            definition_of_ready=body.definition_of_ready,
            parent_id=parent_id,
            tags=body.tags,
            actor=_actor(ctx, note),
        )
    except story_store.HumanOnlyError as exc:
        raise ToolError(str(exc)) from exc
    except story_store.ParentNotFoundError:
        raise ToolError(f"Story not found on board {b.key_prefix}: {parent!r}.")
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
        "Update a story's text or tags (partial — omit a field to keep its current value). "
        "Changing the text marks the story stale until it is re-assessed; changing only the tags does not. "
        "A human-only story can't be updated by an agent. "
        "story: story ID or key (e.g. 'FLW-12'). "
        "tags: the story's full new list of tags (replaces the current ones; [] clears them). "
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
    tags: list[str] | None = None,
) -> StoredStoryOut:
    note = _require_note(note)
    s = _resolve_story(story)
    _refuse_human_only(s)
    merged_title = title if title is not None else s.title
    merged_desc = description if description is not None else s.description
    merged_ac = acceptance_criteria if acceptance_criteria is not None else s.acceptance_criteria
    merged_dor = definition_of_ready if definition_of_ready is not None else s.definition_of_ready
    merged_tags = tags if tags is not None else s.tags
    if is_too_large(merged_desc, merged_ac):
        raise ToolError("The story is longer than the size limit (10 000 chars per field).")
    try:
        body = StoryIn(
            title=merged_title,
            description=merged_desc,
            acceptance_criteria=merged_ac,
            definition_of_ready=merged_dor,
            tags=merged_tags,
        )
    except ValidationError as exc:
        raise _invalid(exc) from exc
    try:
        updated = story_store.update_story(
            s.id,
            title=body.title,
            description=body.description,
            acceptance_criteria=body.acceptance_criteria,
            definition_of_ready=body.definition_of_ready,
            tags=body.tags,
            actor=_actor(ctx, note),
        )
    except story_store.HumanOnlyError as exc:
        raise ToolError(str(exc)) from exc
    if updated is None:
        raise ToolError(f"Story not found: {story!r}.")
    return _out(updated)


@_server.tool(
    annotations=_WRITE_IDEMPOTENT,
    description=(
        "Make a story wait on another story on the same board: it can't move into in_sprint or done until that "
        "story is done. Refused if it would make a story wait on itself, directly or through other stories. "
        "Adding a dependency that exists already changes nothing. A human-only story can't be changed by an agent. "
        "story: the story that waits, ID or key (e.g. 'FLW-12'). "
        "blocker: the story it waits on, ID or key (e.g. 'FLW-3'). "
        "note: required — why it needs that story first."
    ),
)
def add_blocker(story: str, blocker: str, note: Note, ctx: Context) -> StoredStoryOut:
    note = _require_note(note)
    s = _resolve_story(story)
    _refuse_human_only(s)
    b = _resolve_story(blocker)
    if b.board_id != s.board_id:
        raise ToolError(f"{b.key} is not on the same board as {s.key}; a story can only wait on stories on its board.")
    try:
        updated = story_store.add_blocker(s.id, b.id, actor=_actor(ctx, note))
    except (story_store.DependencyCycleError, story_store.HumanOnlyError) as exc:
        raise ToolError(str(exc)) from exc
    except story_store.BlockerNotFoundError:
        raise ToolError(f"Story not found on the board of {s.key}: {blocker!r}.")
    if updated is None:
        raise ToolError(f"Story not found: {story!r}.")
    return _out(updated)


@_server.tool(
    annotations=_WRITE_IDEMPOTENT,
    description=(
        "Stop a story waiting on another story. Removing a dependency that does not exist changes nothing. "
        "A human-only story can't be changed by an agent. "
        "story: the story that waits, ID or key (e.g. 'FLW-12'). "
        "blocker: the story it no longer waits on, ID or key (e.g. 'FLW-3'). "
        "note: required — why it no longer needs that story first."
    ),
)
def remove_blocker(story: str, blocker: str, note: Note, ctx: Context) -> StoredStoryOut:
    note = _require_note(note)
    s = _resolve_story(story)
    _refuse_human_only(s)
    b = _resolve_story(blocker)
    try:
        updated = story_store.remove_blocker(s.id, b.id, actor=_actor(ctx, note))
    except story_store.HumanOnlyError as exc:
        raise ToolError(str(exc)) from exc
    if updated is None:
        raise ToolError(f"Story not found: {story!r}.")
    return _out(updated)


class EvidenceItem(BaseModel):
    path: str | None = Field(
        None, description="Absolute path of a local file: a test report (.html .xml .json .txt .log .pdf) or a "
        "screenshot or recording (.png .jpg .jpeg .gif .webp .mp4 .webm .mov), at most 50 MB."
    )
    url: str | None = Field(None, description="Link to evidence kept elsewhere, e.g. a CI run. Give path or url.")
    caption: str | None = Field(None, description="Optional short summary, e.g. '42 passed'.")


class TestReportItem(EvidenceItem):
    __test__ = False  # not a pytest test class

    kind: Literal["unit", "integration", "cucumber"]


class DoneEvidence(BaseModel):
    test_reports: list[TestReportItem] = Field(description="At least one test report.")
    ui_change: bool = Field(description="True if the story changes the UI.")
    ui_evidence: list[EvidenceItem] = Field(
        default_factory=list, description="Screenshots or recordings; at least one when ui_change is true."
    )
    commits: list[CommitIn] = Field(description="At least one commit: hash (7–40 hex) and message.")


def _evidence_in(evidence: DoneEvidence) -> DoneEvidenceIn:
    """Validate the evidence's shape before any file is read; each path stands in as its file_id until uploaded."""

    def item(entry: EvidenceItem) -> dict:
        out = entry.model_dump(exclude={"path"})
        out["file_id"] = entry.path
        return out

    try:
        return DoneEvidenceIn(
            test_reports=[item(r) for r in evidence.test_reports],
            ui_change=evidence.ui_change,
            ui_evidence=[item(e) for e in evidence.ui_evidence],
            commits=evidence.commits,
        )
    except ValidationError as exc:
        raise _invalid(exc) from exc


def _read_evidence_file(path_str: str) -> tuple[str, bytes]:
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        raise ToolError(f"Evidence path must be absolute: {path_str!r}.")
    if not path.is_file():
        raise ToolError(f"Evidence file not found: {path_str!r}.")
    try:
        story_store.evidence_content_type(path.name)
    except story_store.EvidenceFileTypeError:
        raise ToolError(f"Evidence file type not allowed: {path.name!r}. Use one of: {' '.join(story_store.EVIDENCE_TYPES)}.")
    if path.stat().st_size > story_store.MAX_EVIDENCE_BYTES:
        raise ToolError(f"Evidence file is larger than {story_store.MAX_EVIDENCE_BYTES // (1024 * 1024)} MB: {path_str!r}.")
    return path.name, path.read_bytes()


def _upload_paths(story_id: str, evidence: DoneEvidenceIn, saved: list[str], actor: story_store.Actor) -> None:
    """Upload every path standing in as a file_id and swap in the real ID. Appends each new file's ID to *saved*."""
    for entry in [*evidence.test_reports, *evidence.ui_evidence]:
        if entry.file_id is None:
            continue
        name, data = _read_evidence_file(entry.file_id)
        file = story_store.save_evidence_file(story_id, name, data, actor=actor)
        if file is None:
            raise ToolError("Story not found.")
        saved.append(file.id)
        entry.file_id = file.id


@_server.tool(
    annotations=_WRITE,
    description=(
        "Move a story to a different workflow column and position. "
        "story: story ID or key (e.g. 'FLW-12'). "
        "status: one of backlog/refinement/ready_for_sprint/in_sprint/done/blocked. "
        "place: 'top' (position = min - 1) or 'bottom' (position = max + 1, default). "
        "blocked_reason: required when moving to 'blocked'; not allowed otherwise. "
        "A story whose latest verdict is discuss or needs_refinement can only move to backlog or refinement "
        "until it is assessed again. "
        "A story can't move into in_sprint or done while a story it waits on (blocked_by) is not done. "
        "done_evidence: required when moving INTO 'done'; not allowed otherwise. Test reports and "
        "screenshots/recordings are absolute local file paths (uploaded for you) or URLs. "
        "A human-only story can't be moved by an agent. "
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
    done_evidence: DoneEvidence | None = None,
) -> StoredStoryOut:
    note = _require_note(note)
    s = _resolve_story(story)
    _refuse_human_only(s)  # before any evidence file is read or uploaded
    evidence = _evidence_in(done_evidence) if done_evidence is not None else None
    # Validate via Pydantic so the Blocked and Done rules fire.
    try:
        body = StoryMoveIn(
            status=status,
            position=0,  # placeholder; we compute the real one below
            blocked_reason=blocked_reason,
            done_evidence=evidence,
        )
    except ValidationError as exc:
        raise _invalid(exc) from exc
    if status == "done" and s.status != "done" and evidence is None:
        raise ToolError(
            "Moving a story to done needs done_evidence: at least one test report, a screenshot or recording if it "
            "changes the UI, and at least one commit."
        )
    # Same rule as positionBetween() in the board UI: one before the first card, or one after the last.
    bounds = story_store.column_bounds(s.board_id, body.status)
    if bounds is None:
        position = 1.0
    else:
        position = bounds[0] - 1 if place == "top" else bounds[1] + 1
    saved: list[str] = []
    try:
        if evidence is not None:
            _upload_paths(s.id, evidence, saved, _actor(ctx, note))
        updated = story_store.move_story(
            s.id,
            status=body.status,
            position=position,
            blocked_reason=body.blocked_reason,
            done_evidence=evidence.model_dump() if evidence is not None else None,
            actor=_actor(ctx, note),
        )
    except (
        story_store.EvidenceError, story_store.ReadinessError, story_store.BlockersOpenError, story_store.HumanOnlyError
    ) as exc:
        _discard(saved)
        raise ToolError(str(exc)) from exc
    except BaseException:
        _discard(saved)
        raise
    if updated is None:
        _discard(saved)
        raise ToolError(f"Story not found: {story!r}.")
    return _out(updated)


def _discard(file_ids: list[str]) -> None:
    """Delete evidence files uploaded for a move that did not happen."""
    for file_id in file_ids:
        story_store.delete_unattached_evidence_file(file_id)


@_server.tool(
    annotations=_WRITE,
    description=(
        "Assess a story: runs the TypeSafe/Jev model and saves the report. A human-only story can't be assessed by an "
        "agent. "
        "story: story ID or key (e.g. 'FLW-12'). "
        "note: required — why you are assessing this story (e.g. 'Checking readiness before sprint planning')."
    ),
)
async def assess_story(story: str, note: Note, ctx: Context) -> StoryDetailMcpOut:
    note = _require_note(note)
    s = _resolve_story(story)
    _refuse_human_only(s)  # before paying for a Jev call
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
    except story_store.HumanOnlyError as exc:
        raise ToolError(str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise ToolError("Story was deleted during assessment.") from exc
    fresh = story_store.get_story(s.id)
    if fresh is None:
        raise ToolError("Story was deleted during assessment.")
    return _detail(fresh)


def main() -> None:
    story_store.init_db()
    # Show which database this agent writes to: under uvx a relative path lands in the client's working directory
    logger.info("Story database: %s", story_store._db_path().resolve())
    _server.run("stdio")


if __name__ == "__main__":
    main()
