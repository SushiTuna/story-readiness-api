"""SQLite store for boards, their pasted stories, and the stories' assessment history.

Uses the standard-library sqlite3 module with one short-lived connection per
call, so the functions are safe to run from FastAPI's threadpool. The database
file comes from STORIES_DB_PATH (default data/stories.db).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.linear_client import author_questions, issue_fingerprint
from app.schemas import ReportOut

DEFAULT_DB_PATH = "data/stories.db"

# Workflow columns of the board, in order. Stories start in the backlog.
STATUSES = ("backlog", "refinement", "ready_for_sprint", "in_sprint", "done", "blocked")

# While its latest verdict (stale or not) is one of these, a story may only move into REFINING_STATUSES, or within
# the column it is already in. Assessing it again is the only way to lift this.
REFINING_VERDICTS = {"discuss": "Discuss", "needs_refinement": "Needs refinement"}
REFINING_STATUSES = ("backlog", "refinement")

# Each board is scoped to one goal and holds at most this many stories, whatever their column.
MAX_STORIES_PER_BOARD = 100

# Human-readable story keys: <board prefix>-<number>, e.g. FLW-1. Numbers come from a counter per prefix, so a
# deleted story's number is never reused, not even by a later board with the same prefix.
# Stories from before boards existed move to a board with this prefix, so their keys stay the same.
LEGACY_KEY_PREFIX = "ST"
LEGACY_BOARD_NAME = "Stories"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS boards (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    key_prefix  TEXT NOT NULL UNIQUE,  -- fixed once created, so story keys stay stable
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stories (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    description         TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL,
    definition_of_ready TEXT NOT NULL,  -- JSON list of strings
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'backlog',  -- workflow column on the board
    position            REAL NOT NULL DEFAULT 0,          -- order within the column, ascending
    number              INTEGER,                          -- sequential key number (<prefix>-<number>), unique per board
    blocked_reason      TEXT,                             -- why it is blocked; only set while status = 'blocked'
    board_id            TEXT REFERENCES boards(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,  -- 'prefix:<key prefix>'
    value INTEGER NOT NULL   -- last number handed out
);
CREATE TABLE IF NOT EXISTS assessments (
    id             INTEGER PRIMARY KEY,
    story_id       TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    verdict        TEXT NOT NULL,
    quality        REAL NOT NULL,
    question_count INTEGER NOT NULL,
    fingerprint    TEXT NOT NULL,  -- story_fingerprint() of the assessed text
    report_json    TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS assessments_story ON assessments(story_id, id);
CREATE TABLE IF NOT EXISTS activity (
    id         INTEGER PRIMARY KEY,
    board_id   TEXT NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
    story_id   TEXT REFERENCES stories(id) ON DELETE SET NULL,
    story_key  TEXT,
    actor_kind TEXT NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_story ON activity(story_id, id);
CREATE INDEX IF NOT EXISTS activity_board ON activity(board_id, id);
CREATE TABLE IF NOT EXISTS evidence_files (
    id           TEXT PRIMARY KEY,  -- also the file's name on disk, under evidence/<story_id>/
    story_id     TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,     -- the uploaded name, shown and used for downloads
    content_type TEXT NOT NULL,
    size         INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    attached     INTEGER NOT NULL DEFAULT 0  -- 1 once a move to Done uses it; attached files can't be deleted
);
CREATE INDEX IF NOT EXISTS evidence_files_story ON evidence_files(story_id);
"""


class BoardNotFoundError(Exception):
    """The board does not exist."""


class BoardFullError(Exception):
    """The board already holds MAX_STORIES_PER_BOARD stories."""


class DuplicateKeyPrefixError(Exception):
    """Another board already uses this key prefix."""


class EvidenceError(Exception):
    """A move to Done is missing its evidence, or the evidence refers to files it can't use."""


class ReadinessError(Exception):
    """The story's latest verdict keeps it in Backlog or Refinement until it is assessed again."""


class EvidenceFileTypeError(Exception):
    """The file's extension is not one of EVIDENCE_TYPES."""


class EvidenceFileTooLargeError(Exception):
    """The file is larger than MAX_EVIDENCE_BYTES."""


class EvidenceFileAttachedError(Exception):
    """The file is part of a move to Done, so it is kept."""


# Evidence files: test reports, and screenshots or recordings of UI changes. The content type comes from the
# extension, never from the client, so a file is always served as what its name says.
EVIDENCE_TYPES = {
    ".html": "text/html",
    ".xml": "application/xml",
    ".json": "application/json",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}
MAX_EVIDENCE_BYTES = 50 * 1024 * 1024


@dataclass
class Actor:
    kind: str   # 'agent' | 'user'
    name: str
    note: str | None = None


BOARD_USER = Actor("user", "board")


@dataclass
class Board:
    id: str
    name: str
    description: str
    key_prefix: str
    created_at: datetime
    story_count: int = 0


@dataclass
class StoredStory:
    id: str
    title: str
    description: str
    acceptance_criteria: str
    definition_of_ready: list[str]
    created_at: datetime
    updated_at: datetime
    status: str = "backlog"
    position: float = 0.0
    number: int = 0
    blocked_reason: str | None = None
    board_id: str = ""
    key_prefix: str = LEGACY_KEY_PREFIX

    @property
    def key(self) -> str:
        return f"{self.key_prefix}-{self.number}"

    @property
    def fingerprint(self) -> str:
        return story_fingerprint(self.title, self.description, self.acceptance_criteria, self.definition_of_ready)


@dataclass
class Assessment:
    story_id: str
    verdict: str
    quality: float
    question_count: int
    fingerprint: str
    created_at: datetime
    report_json: str | None = None  # left out of list queries

    def report(self) -> ReportOut | None:
        return ReportOut.model_validate_json(self.report_json) if self.report_json else None


@dataclass
class EvidenceFile:
    id: str
    story_id: str
    filename: str
    content_type: str
    size: int
    created_at: datetime
    attached: bool = False


@dataclass
class ActivityEntry:
    id: int
    board_id: str
    story_id: str | None
    story_key: str | None
    actor_kind: str
    actor: str
    action: str
    detail: dict
    note: str | None
    created_at: datetime


def story_fingerprint(title: str, description: str, acceptance_criteria: str, definition_of_ready: list[str]) -> str:
    """Hash of everything the assessment reads; a change marks the latest assessment stale."""
    return issue_fingerprint(title, "\n".join([description, acceptance_criteria, *definition_of_ready]))


def _db_path() -> Path:
    return Path(os.getenv("STORIES_DB_PATH") or DEFAULT_DB_PATH)  # blank counts as unset


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:  # commits on success, rolls back on error; does not close
            yield conn
    finally:
        conn.close()


def _now() -> datetime:
    return datetime.now(UTC)


# Every story read joins its board for the key prefix.
_SELECT_STORY = "SELECT stories.*, boards.key_prefix FROM stories JOIN boards ON boards.id = stories.board_id"


def _board(row: sqlite3.Row) -> Board:
    return Board(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        key_prefix=row["key_prefix"],
        created_at=datetime.fromisoformat(row["created_at"]),
        story_count=row["story_count"],
    )


def _story(row: sqlite3.Row) -> StoredStory:
    return StoredStory(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        acceptance_criteria=row["acceptance_criteria"],
        definition_of_ready=json.loads(row["definition_of_ready"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        status=row["status"],
        position=row["position"],
        number=row["number"],
        blocked_reason=row["blocked_reason"],
        board_id=row["board_id"],
        key_prefix=row["key_prefix"],
    )


def _assessment(row: sqlite3.Row) -> Assessment:
    return Assessment(
        story_id=row["story_id"],
        verdict=row["verdict"],
        quality=row["quality"],
        question_count=row["question_count"],
        fingerprint=row["fingerprint"],
        created_at=datetime.fromisoformat(row["created_at"]),
        report_json=row["report_json"] if "report_json" in row.keys() else None,
    )


def _activity(row: sqlite3.Row) -> ActivityEntry:
    return ActivityEntry(
        id=row["id"],
        board_id=row["board_id"],
        story_id=row["story_id"],
        story_key=row["story_key"],
        actor_kind=row["actor_kind"],
        actor=row["actor"],
        action=row["action"],
        detail=json.loads(row["detail"]),
        note=row["note"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _evidence_file(row: sqlite3.Row) -> EvidenceFile:
    return EvidenceFile(
        id=row["id"],
        story_id=row["story_id"],
        filename=row["filename"],
        content_type=row["content_type"],
        size=row["size"],
        created_at=datetime.fromisoformat(row["created_at"]),
        attached=bool(row["attached"]),
    )


def _log_activity(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    story_id: str | None,
    story_key: str | None,
    actor: Actor,
    action: str,
    detail: dict,
) -> None:
    conn.execute(
        """
        INSERT INTO activity (board_id, story_id, story_key, actor_kind, actor, action, detail, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            board_id,
            story_id,
            story_key,
            actor.kind,
            actor.name,
            action,
            json.dumps(detail),
            actor.note,
            _now().isoformat(),
        ),
    )


def init_db() -> None:
    """Create the tables if they do not exist, and add columns missing from older databases."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(stories)")}
        if "status" not in columns:
            conn.execute("ALTER TABLE stories ADD COLUMN status TEXT NOT NULL DEFAULT 'backlog'")
        if "position" not in columns:
            conn.execute("ALTER TABLE stories ADD COLUMN position REAL NOT NULL DEFAULT 0")
        if "number" not in columns:
            conn.execute("ALTER TABLE stories ADD COLUMN number INTEGER")
        if "blocked_reason" not in columns:
            conn.execute("ALTER TABLE stories ADD COLUMN blocked_reason TEXT")
        if "board_id" not in columns:
            conn.execute("ALTER TABLE stories ADD COLUMN board_id TEXT REFERENCES boards(id) ON DELETE CASCADE")
        # Stories from before boards existed move to one board whose prefix keeps their ST-<n> keys.
        if conn.execute("SELECT 1 FROM stories WHERE board_id IS NULL LIMIT 1").fetchone():
            legacy = conn.execute("SELECT id FROM boards WHERE key_prefix = ?", (LEGACY_KEY_PREFIX,)).fetchone()
            board_id = legacy["id"] if legacy else _insert_board(conn, LEGACY_BOARD_NAME, LEGACY_KEY_PREFIX, "").id
            conn.execute("UPDATE stories SET board_id = ? WHERE board_id IS NULL", (board_id,))
        # The single counter from before boards now belongs to the legacy prefix.
        conn.execute(
            """
            INSERT INTO counters (name, value) SELECT ?, value FROM counters WHERE name = 'story'
            ON CONFLICT (name) DO UPDATE SET value = MAX(value, excluded.value)
            """,
            (_counter(LEGACY_KEY_PREFIX),),
        )
        conn.execute("DELETE FROM counters WHERE name = 'story'")
        # Number stories created before keys existed, oldest first.
        unnumbered = conn.execute(
            """
            SELECT stories.id, stories.board_id, boards.key_prefix FROM stories JOIN boards ON boards.id = stories.board_id
            WHERE number IS NULL ORDER BY stories.created_at, stories.rowid
            """
        ).fetchall()
        for row in unnumbered:
            number = _next_number(conn, row["board_id"], row["key_prefix"])
            conn.execute("UPDATE stories SET number = ? WHERE id = ?", (number, row["id"]))
        # Numbers are unique per board now, not across all stories.
        conn.execute("DROP INDEX IF EXISTS stories_number")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS stories_board_number ON stories(board_id, number)")


def _counter(key_prefix: str) -> str:
    return f"prefix:{key_prefix}"


def _next_number(conn: sqlite3.Connection, board_id: str, key_prefix: str) -> int:
    """Hand out the next story number for a board. Runs inside the caller's transaction, so concurrent creates can't collide."""
    (highest,) = conn.execute("SELECT COALESCE(MAX(number), 0) FROM stories WHERE board_id = ?", (board_id,)).fetchone()
    # MAX(value, highest) keeps the counter ahead of numbers already in the table (e.g. an older database).
    (number,) = conn.execute(
        """
        INSERT INTO counters (name, value) VALUES (?, ? + 1)
        ON CONFLICT (name) DO UPDATE SET value = MAX(value, ?) + 1
        RETURNING value
        """,
        (_counter(key_prefix), highest, highest),
    ).fetchone()
    return number


# ---------------------------------------------------------------------------
# Boards
# ---------------------------------------------------------------------------

_SELECT_BOARD = """
SELECT boards.*, (SELECT COUNT(*) FROM stories WHERE stories.board_id = boards.id) AS story_count FROM boards
"""


def _insert_board(conn: sqlite3.Connection, name: str, key_prefix: str, description: str) -> Board:
    board = Board(id=str(uuid.uuid4()), name=name, description=description, key_prefix=key_prefix, created_at=_now())
    conn.execute(
        "INSERT INTO boards (id, name, description, key_prefix, created_at) VALUES (?, ?, ?, ?, ?)",
        (board.id, board.name, board.description, board.key_prefix, board.created_at.isoformat()),
    )
    return board


def create_board(*, name: str, key_prefix: str, description: str = "", actor: Actor = BOARD_USER) -> Board:
    """Create an empty board. Raises DuplicateKeyPrefixError if another board uses *key_prefix*."""
    try:
        with _connect() as conn:
            board = _insert_board(conn, name, key_prefix, description)
            _log_activity(
                conn,
                board_id=board.id,
                story_id=None,
                story_key=None,
                actor=actor,
                action="board_created",
                detail={"name": name, "key_prefix": key_prefix},
            )
            return board
    except sqlite3.IntegrityError as exc:
        raise DuplicateKeyPrefixError(key_prefix) from exc


def list_boards() -> list[Board]:
    """Every board, oldest first, with its story count."""
    with _connect() as conn:
        return [_board(r) for r in conn.execute(f"{_SELECT_BOARD} ORDER BY boards.created_at, boards.rowid")]


def get_board(board_id: str) -> Board | None:
    with _connect() as conn:
        row = conn.execute(f"{_SELECT_BOARD} WHERE boards.id = ?", (board_id,)).fetchone()
    return _board(row) if row else None


def get_board_by_prefix(prefix: str) -> Board | None:
    """Look up a board by its key prefix (e.g. 'FLW')."""
    with _connect() as conn:
        row = conn.execute(
            f"{_SELECT_BOARD} WHERE boards.key_prefix = ?", (prefix.upper(),)
        ).fetchone()
    return _board(row) if row else None


def update_board(board_id: str, *, name: str, description: str, actor: Actor = BOARD_USER) -> Board | None:
    """Rename a board or change its description. The key prefix never changes.

    Logs the changed fields' old and new values; a call that changes nothing is not logged.
    """
    with _connect() as conn:
        old = conn.execute("SELECT name, description FROM boards WHERE id = ?", (board_id,)).fetchone()
        if old is None:
            return None
        new = {"name": name, "description": description}
        changed = [f for f in new if old[f] != new[f]]
        conn.execute("UPDATE boards SET name = ?, description = ? WHERE id = ?", (name, description, board_id))
        row = conn.execute(f"{_SELECT_BOARD} WHERE boards.id = ?", (board_id,)).fetchone()
        if changed:
            _log_activity(
                conn,
                board_id=board_id,
                story_id=None,
                story_key=None,
                actor=actor,
                action="board_updated",
                detail={"from": {f: old[f] for f in changed}, "to": {f: new[f] for f in changed}},
            )
    return _board(row)


def delete_board(board_id: str) -> bool:
    """Delete a board and, through ON DELETE CASCADE, its stories and their assessments.

    The prefix's counter is kept, so a later board with the same prefix does not reuse a key.
    Returns False if the board did not exist.
    """
    with _connect() as conn:
        story_ids = [r["id"] for r in conn.execute("SELECT id FROM stories WHERE board_id = ?", (board_id,))]
        cur = conn.execute("DELETE FROM boards WHERE id = ?", (board_id,))
    for story_id in story_ids:
        _remove_evidence_dir(story_id)
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Stories
# ---------------------------------------------------------------------------


def create_story(
    board_id: str,
    *,
    title: str,
    description: str,
    acceptance_criteria: str,
    definition_of_ready: list[str],
    actor: Actor = BOARD_USER,
) -> StoredStory:
    """Add a story to the end of a board's backlog.

    Raises BoardNotFoundError if the board does not exist, and BoardFullError if it already holds
    MAX_STORIES_PER_BOARD stories.
    """
    now = _now()
    story = StoredStory(
        id=str(uuid.uuid4()),
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
        definition_of_ready=definition_of_ready,
        created_at=now,
        updated_at=now,
        board_id=board_id,
    )
    with _connect() as conn:
        # Take the write lock before reading, so two creates can't both see room for the last story.
        conn.execute("BEGIN IMMEDIATE")
        board = conn.execute("SELECT key_prefix FROM boards WHERE id = ?", (board_id,)).fetchone()
        if board is None:
            raise BoardNotFoundError(board_id)
        (count,) = conn.execute("SELECT COUNT(*) FROM stories WHERE board_id = ?", (board_id,)).fetchone()
        if count >= MAX_STORIES_PER_BOARD:
            raise BoardFullError(board_id)
        story.key_prefix = board["key_prefix"]
        # New stories go to the end of the board's backlog.
        (last,) = conn.execute(
            "SELECT MAX(position) FROM stories WHERE board_id = ? AND status = 'backlog'", (board_id,)
        ).fetchone()
        story.position = (last or 0) + 1
        story.number = _next_number(conn, board_id, story.key_prefix)
        conn.execute(
            """
            INSERT INTO stories
                (id, title, description, acceptance_criteria, definition_of_ready, created_at, updated_at, status, position,
                 number, board_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                story.id,
                story.title,
                story.description,
                story.acceptance_criteria,
                json.dumps(story.definition_of_ready),
                now.isoformat(),
                now.isoformat(),
                story.status,
                story.position,
                story.number,
                story.board_id,
            ),
        )
        _log_activity(
            conn,
            board_id=board_id,
            story_id=story.id,
            story_key=story.key,
            actor=actor,
            action="story_created",
            detail={"title": title},
        )
    return story


def get_story(story_id: str) -> StoredStory | None:
    with _connect() as conn:
        row = conn.execute(f"{_SELECT_STORY} WHERE stories.id = ?", (story_id,)).fetchone()
    return _story(row) if row else None


def get_story_by_key(key: str) -> StoredStory | None:
    """Look up a story by its human-readable key, e.g. 'FLW-12'."""
    parts = key.split("-", 1)
    if len(parts) != 2:
        return None
    prefix, num_str = parts
    try:
        number = int(num_str)
    except ValueError:
        return None
    with _connect() as conn:
        row = conn.execute(
            f"""
            {_SELECT_STORY}
            WHERE boards.key_prefix = ? AND stories.number = ?
            """,
            (prefix.upper(), number),
        ).fetchone()
    return _story(row) if row else None


def list_stories(board_id: str) -> list[tuple[StoredStory, list[Assessment]]]:
    """Return a board's stories in board order (position), with their latest two assessments (newest first)."""
    with _connect() as conn:
        stories = [
            _story(r)
            for r in conn.execute(
                f"{_SELECT_STORY} WHERE stories.board_id = ? ORDER BY stories.position, stories.created_at", (board_id,)
            )
        ]
        rows = conn.execute(
            """
            SELECT story_id, verdict, quality, question_count, fingerprint, created_at FROM (
                SELECT assessments.*, ROW_NUMBER() OVER (PARTITION BY story_id ORDER BY assessments.id DESC) AS rn
                FROM assessments JOIN stories ON stories.id = assessments.story_id
                WHERE stories.board_id = ?
            ) WHERE rn <= 2 ORDER BY story_id, rn
            """,
            (board_id,),
        ).fetchall()
    recent: dict[str, list[Assessment]] = {}
    for row in rows:
        recent.setdefault(row["story_id"], []).append(_assessment(row))
    return [(story, recent.get(story.id, [])) for story in stories]


def agents_by_story(board_id: str) -> dict[str, list[str]]:
    """Return distinct agent names per story_id (oldest first) for a board.

    Only includes entries from actors with kind='agent'.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT story_id, actor FROM activity
            WHERE board_id = ? AND actor_kind = 'agent' AND story_id IS NOT NULL
            ORDER BY id ASC
            """,
            (board_id,),
        ).fetchall()
    result: dict[str, list[str]] = {}
    for row in rows:
        sid = row["story_id"]
        name = row["actor"]
        if sid not in result:
            result[sid] = []
        if name not in result[sid]:
            result[sid].append(name)
    return result


def column_bounds(board_id: str, status: str) -> tuple[float, float] | None:
    """Lowest and highest position in one column of a board, or None if the column is empty."""
    with _connect() as conn:
        low, high = conn.execute(
            "SELECT MIN(position), MAX(position) FROM stories WHERE board_id = ? AND status = ?", (board_id, status)
        ).fetchone()
    return None if low is None else (low, high)


def update_story(
    story_id: str,
    *,
    title: str,
    description: str,
    acceptance_criteria: str,
    definition_of_ready: list[str],
    actor: Actor = BOARD_USER,
) -> StoredStory | None:
    with _connect() as conn:
        # Read current values to compute changed fields.
        old_row = conn.execute(f"{_SELECT_STORY} WHERE stories.id = ?", (story_id,)).fetchone()
        if old_row is None:
            return None
        old = _story(old_row)
        changed = []
        if old.title != title:
            changed.append("title")
        if old.description != description:
            changed.append("description")
        if old.acceptance_criteria != acceptance_criteria:
            changed.append("acceptance_criteria")
        if old.definition_of_ready != definition_of_ready:
            changed.append("definition_of_ready")
        cur = conn.execute(
            """
            UPDATE stories
            SET title = ?, description = ?, acceptance_criteria = ?, definition_of_ready = ?, updated_at = ?
            WHERE id = ?
            """,
            (title, description, acceptance_criteria, json.dumps(definition_of_ready), _now().isoformat(), story_id),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(f"{_SELECT_STORY} WHERE stories.id = ?", (story_id,)).fetchone()
        if changed:  # saving unchanged text is not an edit worth logging
            _log_activity(
                conn,
                board_id=old.board_id,
                story_id=story_id,
                story_key=old.key,
                actor=actor,
                action="story_edited",
                detail={"fields": changed},
            )
    return _story(row)


def move_story(
    story_id: str,
    *,
    status: str,
    position: float,
    blocked_reason: str | None = None,
    done_evidence: dict | None = None,
    actor: Actor = BOARD_USER,
) -> StoredStory | None:
    """Put a story in a workflow column at *position*. Does not touch updated_at: moving is not an edit.

    *blocked_reason* is kept only for the blocked column; any other column clears it.

    A story whose latest verdict is in REFINING_VERDICTS, even a stale one, may only move into REFINING_STATUSES or
    within its current column. Raises ReadinessError.

    Entering the done column needs *done_evidence* (DoneEvidenceIn as a dict); moving within it does not, and no
    other move takes it. Its uploaded files must belong to the story and not be used yet. They are marked attached,
    and the evidence, with each file's name, type and size, goes into the activity entry. Raises EvidenceError.
    """
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status}")
    reason = blocked_reason if status == "blocked" else None
    with _connect() as conn:
        # Read old status before updating.
        old_row = conn.execute(f"{_SELECT_STORY} WHERE stories.id = ?", (story_id,)).fetchone()
        if old_row is None:
            return None
        old_status = old_row["status"]
        board_id = old_row["board_id"]
        story_key = f"{old_row['key_prefix']}-{old_row['number']}"
        if status != old_status and status not in REFINING_STATUSES:
            latest = conn.execute(
                "SELECT verdict FROM assessments WHERE story_id = ? ORDER BY id DESC LIMIT 1", (story_id,)
            ).fetchone()
            if latest is not None and latest["verdict"] in REFINING_VERDICTS:
                raise ReadinessError(
                    f"{story_key} has a {REFINING_VERDICTS[latest['verdict']]} verdict, so it can only move to Backlog "
                    "or Refinement. Assess it again once it is refined."
                )
        entering_done = status == "done" and old_status != "done"
        if entering_done and done_evidence is None:
            raise EvidenceError(
                "Moving a story to Done needs done_evidence: at least one test report, a screenshot or recording "
                "if it changes the UI, and at least one commit."
            )
        if done_evidence is not None and not entering_done:
            raise EvidenceError("done_evidence is only accepted when a story enters Done.")
        evidence = _attach_evidence(conn, story_id, done_evidence) if done_evidence is not None else None
        cur = conn.execute(
            "UPDATE stories SET status = ?, position = ?, blocked_reason = ? WHERE id = ?",
            (status, position, reason, story_id),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(f"{_SELECT_STORY} WHERE stories.id = ?", (story_id,)).fetchone()
        detail: dict = {"from": old_status, "to": status}
        if reason:
            detail["blocked_reason"] = reason
        if evidence is not None:
            detail["evidence"] = evidence
        _log_activity(
            conn,
            board_id=board_id,
            story_id=story_id,
            story_key=story_key,
            actor=actor,
            action="story_moved",
            detail=detail,
        )
    return _story(row)


def delete_story(story_id: str, actor: Actor = BOARD_USER) -> bool:
    """Delete a story and, through ON DELETE CASCADE, its assessments. Returns False if it did not exist."""
    with _connect() as conn:
        # Read the key first: the story's activity rows keep it after ON DELETE SET NULL clears their story_id.
        row = conn.execute(f"{_SELECT_STORY} WHERE stories.id = ?", (story_id,)).fetchone()
        if row is None:
            return False
        board_id = row["board_id"]
        story_key = f"{row['key_prefix']}-{row['number']}"
        cur = conn.execute("DELETE FROM stories WHERE id = ?", (story_id,))
        if cur.rowcount == 0:
            return False
        _log_activity(
            conn,
            board_id=board_id,
            story_id=None,
            story_key=story_key,
            actor=actor,
            action="story_deleted",
            detail={"key": story_key},
        )
    _remove_evidence_dir(story_id)
    return True


def add_assessment(story: StoredStory, report: ReportOut, actor: Actor = BOARD_USER) -> Assessment:
    """Save *report* as the latest assessment of *story*, fingerprinting the text that was assessed."""
    questions, _notes = author_questions(report)
    assessment = Assessment(
        story_id=story.id,
        verdict=report.verdict,
        quality=report.quality,
        question_count=len(questions),
        fingerprint=story.fingerprint,
        created_at=_now(),
        report_json=report.model_dump_json(by_alias=True),
    )
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO assessments (story_id, verdict, quality, question_count, fingerprint, report_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                assessment.story_id,
                assessment.verdict,
                assessment.quality,
                assessment.question_count,
                assessment.fingerprint,
                assessment.report_json,
                assessment.created_at.isoformat(),
            ),
        )
        # Look up board_id and story_key for the activity log.
        story_row = conn.execute(
            f"{_SELECT_STORY} WHERE stories.id = ?", (story.id,)
        ).fetchone()
        if story_row is not None:
            _log_activity(
                conn,
                board_id=story_row["board_id"],
                story_id=story.id,
                story_key=f"{story_row['key_prefix']}-{story_row['number']}",
                actor=actor,
                action="story_assessed",
                detail={"verdict": report.verdict, "quality": report.quality},
            )
    return assessment


def list_assessments(story_id: str) -> list[Assessment]:
    """Every assessment of a story, newest first, including the full report JSON."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM assessments WHERE story_id = ? ORDER BY id DESC", (story_id,)
        ).fetchall()
    return [_assessment(r) for r in rows]


def list_activity(board_id: str, story_id: str | None = None, limit: int = 100) -> list[ActivityEntry]:
    """Activity entries for a board (or a specific story), newest first."""
    with _connect() as conn:
        if story_id is not None:
            rows = conn.execute(
                "SELECT * FROM activity WHERE board_id = ? AND story_id = ? ORDER BY id DESC LIMIT ?",
                (board_id, story_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM activity WHERE board_id = ? ORDER BY id DESC LIMIT ?",
                (board_id, limit),
            ).fetchall()
    return [_activity(r) for r in rows]


# ---------------------------------------------------------------------------
# Evidence files
# ---------------------------------------------------------------------------


def _evidence_dir(story_id: str) -> Path:
    """Evidence files live next to the database, one directory per story."""
    return _db_path().parent / "evidence" / story_id


def _remove_evidence_dir(story_id: str) -> None:
    shutil.rmtree(_evidence_dir(story_id), ignore_errors=True)


def evidence_path(file: EvidenceFile) -> Path:
    return _evidence_dir(file.story_id) / file.id


def evidence_content_type(filename: str) -> str:
    """The content type for *filename*'s extension. Raises EvidenceFileTypeError for any other extension."""
    content_type = EVIDENCE_TYPES.get(Path(filename).suffix.lower())
    if content_type is None:
        raise EvidenceFileTypeError(filename)
    return content_type


def save_evidence_file(story_id: str, filename: str, data: bytes) -> EvidenceFile | None:
    """Store an uploaded evidence file for a story; a move to Done can then use it by ID.

    Returns None if the story does not exist. Raises EvidenceFileTypeError and EvidenceFileTooLargeError.
    """
    name = Path(filename.replace("\\", "/")).name.strip()[:200] or "evidence"
    content_type = evidence_content_type(name)
    if len(data) > MAX_EVIDENCE_BYTES:
        raise EvidenceFileTooLargeError(filename)
    file = EvidenceFile(
        id=str(uuid.uuid4()), story_id=story_id, filename=name, content_type=content_type, size=len(data),
        created_at=_now(),
    )
    with _connect() as conn:
        if conn.execute("SELECT 1 FROM stories WHERE id = ?", (story_id,)).fetchone() is None:
            return None
        conn.execute(
            """
            INSERT INTO evidence_files (id, story_id, filename, content_type, size, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (file.id, file.story_id, file.filename, file.content_type, file.size, file.created_at.isoformat()),
        )
        # Written inside the transaction: if the write fails, the row is rolled back with it.
        path = evidence_path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return file


def get_evidence_file(file_id: str) -> EvidenceFile | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM evidence_files WHERE id = ?", (file_id,)).fetchone()
    return _evidence_file(row) if row else None


def delete_unattached_evidence_file(file_id: str) -> bool:
    """Delete an evidence file no move has used yet. Returns False if it does not exist.

    Raises EvidenceFileAttachedError if a move to Done uses it.
    """
    with _connect() as conn:
        row = conn.execute("SELECT * FROM evidence_files WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            return False
        file = _evidence_file(row)
        if file.attached:
            raise EvidenceFileAttachedError(file_id)
        conn.execute("DELETE FROM evidence_files WHERE id = ?", (file_id,))
    evidence_path(file).unlink(missing_ok=True)
    return True


def _attach_evidence(conn: sqlite3.Connection, story_id: str, evidence: dict) -> dict:
    """Mark the evidence's files attached and return the evidence for the activity log, with file details.

    Runs in move_story's transaction, so a refused move leaves every file unattached.
    """
    seen: set[str] = set()

    def item(entry: dict) -> dict:
        out = {k: entry[k] for k in ("kind", "caption", "url") if entry.get(k) is not None}
        file_id = entry.get("file_id")
        if file_id is None:
            return out
        row = conn.execute("SELECT * FROM evidence_files WHERE id = ?", (file_id,)).fetchone()
        if row is None or row["story_id"] != story_id:
            raise EvidenceError(f"Evidence file {file_id} was not uploaded for this story.")
        if row["attached"] or file_id in seen:
            raise EvidenceError(f"Evidence file {file_id} is already used.")
        seen.add(file_id)
        conn.execute("UPDATE evidence_files SET attached = 1 WHERE id = ?", (file_id,))
        out["file"] = {k: row[k] for k in ("id", "filename", "content_type", "size")}
        return out

    return {
        "test_reports": [item(r) for r in evidence["test_reports"]],
        "ui_change": evidence["ui_change"],
        "ui_evidence": [item(e) for e in evidence.get("ui_evidence", [])],
        "commits": [{"hash": c["hash"], "message": c["message"]} for c in evidence["commits"]],
    }
