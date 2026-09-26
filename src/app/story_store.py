"""SQLite store for boards, their pasted stories, and the stories' assessment history.

Uses the standard-library sqlite3 module with one short-lived connection per
call, so the functions are safe to run from FastAPI's threadpool. The database
file comes from STORIES_DB_PATH (default data/stories.db).
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.linear_client import author_questions, issue_fingerprint
from app.schemas import ReportOut

DEFAULT_DB_PATH = "data/stories.db"

# Workflow columns of the board, in order. Stories start in the backlog.
STATUSES = ("backlog", "refinement", "ready_for_sprint", "in_sprint", "done", "blocked")

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
"""


class BoardNotFoundError(Exception):
    """The board does not exist."""


class BoardFullError(Exception):
    """The board already holds MAX_STORIES_PER_BOARD stories."""


class DuplicateKeyPrefixError(Exception):
    """Another board already uses this key prefix."""


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


def story_fingerprint(title: str, description: str, acceptance_criteria: str, definition_of_ready: list[str]) -> str:
    """Hash of everything the assessment reads; a change marks the latest assessment stale."""
    return issue_fingerprint(title, "\n".join([description, acceptance_criteria, *definition_of_ready]))


def _db_path() -> Path:
    return Path(os.getenv("STORIES_DB_PATH") or DEFAULT_DB_PATH)  # blank counts as unset


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
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


def create_board(*, name: str, key_prefix: str, description: str = "") -> Board:
    """Create an empty board. Raises DuplicateKeyPrefixError if another board uses *key_prefix*."""
    try:
        with _connect() as conn:
            return _insert_board(conn, name, key_prefix, description)
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


def update_board(board_id: str, *, name: str, description: str) -> Board | None:
    """Rename a board or change its description. The key prefix never changes."""
    with _connect() as conn:
        cur = conn.execute("UPDATE boards SET name = ?, description = ? WHERE id = ?", (name, description, board_id))
        if cur.rowcount == 0:
            return None
        row = conn.execute(f"{_SELECT_BOARD} WHERE boards.id = ?", (board_id,)).fetchone()
    return _board(row)


def delete_board(board_id: str) -> bool:
    """Delete a board and, through ON DELETE CASCADE, its stories and their assessments.

    The prefix's counter is kept, so a later board with the same prefix does not reuse a key.
    Returns False if the board did not exist.
    """
    with _connect() as conn:
        cur = conn.execute("DELETE FROM boards WHERE id = ?", (board_id,))
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Stories
# ---------------------------------------------------------------------------


def create_story(
    board_id: str, *, title: str, description: str, acceptance_criteria: str, definition_of_ready: list[str]
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
    return story


def get_story(story_id: str) -> StoredStory | None:
    with _connect() as conn:
        row = conn.execute(f"{_SELECT_STORY} WHERE stories.id = ?", (story_id,)).fetchone()
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


def update_story(
    story_id: str, *, title: str, description: str, acceptance_criteria: str, definition_of_ready: list[str]
) -> StoredStory | None:
    with _connect() as conn:
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
    return _story(row)


def move_story(
    story_id: str, *, status: str, position: float, blocked_reason: str | None = None
) -> StoredStory | None:
    """Put a story in a workflow column at *position*. Does not touch updated_at: moving is not an edit.

    *blocked_reason* is kept only for the blocked column; any other column clears it.
    """
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status}")
    reason = blocked_reason if status == "blocked" else None
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE stories SET status = ?, position = ?, blocked_reason = ? WHERE id = ?",
            (status, position, reason, story_id),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(f"{_SELECT_STORY} WHERE stories.id = ?", (story_id,)).fetchone()
    return _story(row)


def delete_story(story_id: str) -> bool:
    """Delete a story and, through ON DELETE CASCADE, its assessments. Returns False if it did not exist."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM stories WHERE id = ?", (story_id,))
    return cur.rowcount > 0


def add_assessment(story: StoredStory, report: ReportOut) -> Assessment:
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
    return assessment


def list_assessments(story_id: str) -> list[Assessment]:
    """Every assessment of a story, newest first, including the full report JSON."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM assessments WHERE story_id = ? ORDER BY id DESC", (story_id,)
        ).fetchall()
    return [_assessment(r) for r in rows]
