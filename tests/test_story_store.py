"""SQLite story store. conftest.py points STORIES_DB_PATH at a fresh file per test."""
from __future__ import annotations

import sqlite3

import pytest

from app import story_store
from app.schemas import CheckKind, CheckOut
from tests.test_routes import _make_report

_STORY = {
    "title": "Export orders as CSV",
    "description": "As a customer I want to export my orders",
    "acceptance_criteria": "- Given 3 orders, the file has 4 lines",
    "definition_of_ready": ["Security impact is described"],
}


@pytest.fixture(autouse=True)
def st_board():
    """A board with the ST prefix, so keys read ST-1, ST-2, … as before boards existed."""
    return story_store.create_board(name="Stories", key_prefix="ST")


def _st_board() -> story_store.Board:
    """The ST board of the current database (the fixture's, or the one a migration created)."""
    return next(b for b in story_store.list_boards() if b.key_prefix == "ST")


def _create(board_id: str | None = None, **fields) -> story_store.StoredStory:
    return story_store.create_story(board_id or _st_board().id, **{**_STORY, **fields})


def _report(quality: float, *, failing_asks: list[str] | None = None):
    report = _make_report()
    report.quality = quality
    if failing_asks:
        report.checks.append(
            CheckOut(id="failure_handling", label="Failure handling", kind=CheckKind.weighted, value=0.2,
                     passed=False, unsure=False, answer={"yes": 0.2}, ask=failing_asks)
        )
    return report


def test_create_and_get_round_trip():
    created = _create()
    fetched = story_store.get_story(created.id)
    assert fetched == created
    assert fetched.definition_of_ready == ["Security impact is described"]


def test_get_unknown_returns_none():
    assert story_store.get_story("nope") is None


def test_update_changes_text_and_updated_at():
    created = _create()
    updated = story_store.update_story(created.id, **{**_STORY, "title": "New title"})
    assert updated.title == "New title"
    assert updated.created_at == created.created_at
    assert updated.updated_at >= created.updated_at


def test_update_unknown_returns_none():
    assert story_store.update_story("nope", **_STORY) is None


def test_delete_removes_story_and_its_assessments():
    story = _create()
    story_store.add_assessment(story, _report(0.5))
    assert story_store.delete_story(story.id) is True
    assert story_store.get_story(story.id) is None
    assert story_store.list_assessments(story.id) == []
    assert story_store.delete_story(story.id) is False


def test_add_assessment_counts_questions_and_round_trips_report():
    story = _create()
    story_store.add_assessment(story, _report(0.4, failing_asks=["What happens on error?", "Which codes?"]))
    [saved] = story_store.list_assessments(story.id)
    assert saved.question_count == 2
    assert saved.quality == 0.4
    assert saved.report().quality == 0.4
    assert saved.report().jev_model == "jev-1"


def test_list_returns_latest_two_assessments_newest_first():
    story = _create()
    for quality in (0.3, 0.58, 0.96):
        story_store.add_assessment(story, _report(quality))
    [(listed, recent)] = story_store.list_stories(_st_board().id)
    assert listed.id == story.id
    assert [a.quality for a in recent] == [0.96, 0.58]
    assert all(a.report_json is None for a in recent)


def test_list_includes_never_assessed_stories():
    _create()
    [(_, recent)] = story_store.list_stories(_st_board().id)
    assert recent == []


def test_fingerprint_changes_with_any_assessed_field():
    story = _create()
    for field, value in [
        ("title", "Other"),
        ("description", "Other"),
        ("acceptance_criteria", "Other"),
        ("definition_of_ready", ["Other"]),
    ]:
        edited = story_store.update_story(story.id, **{**_STORY, field: value})
        assert edited.fingerprint != story.fingerprint, field


def test_assessment_of_deleted_story_raises_integrity_error():
    story = _create()
    story_store.delete_story(story.id)
    with pytest.raises(sqlite3.IntegrityError):
        story_store.add_assessment(story, _report(0.5))


def test_blank_db_path_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("STORIES_DB_PATH", "")
    assert str(story_store._db_path()) == story_store.DEFAULT_DB_PATH


def test_new_stories_append_to_the_backlog():
    first = _create()
    second = _create()
    assert (first.status, second.status) == ("backlog", "backlog")
    assert second.position > first.position


def test_move_sets_status_and_position_without_touching_updated_at():
    story = _create()
    moved = story_store.move_story(story.id, status="in_sprint", position=2.5)
    assert (moved.status, moved.position) == ("in_sprint", 2.5)
    assert moved.updated_at == story.updated_at
    assert moved.fingerprint == story.fingerprint


def test_move_unknown_story_returns_none_and_bad_status_raises():
    assert story_store.move_story("nope", status="done", position=1) is None
    story = _create()
    with pytest.raises(ValueError):
        story_store.move_story(story.id, status="shipped", position=1)


def test_list_orders_by_position():
    a = _create()
    b = _create()
    story_store.move_story(b.id, status="backlog", position=0.5)
    assert [s.id for s, _ in story_store.list_stories(_st_board().id)] == [b.id, a.id]


def test_init_db_adds_workflow_columns_to_an_older_database(monkeypatch, tmp_path):
    db = tmp_path / "old.db"
    monkeypatch.setenv("STORIES_DB_PATH", str(db))
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE stories (id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
            acceptance_criteria TEXT NOT NULL, definition_of_ready TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO stories VALUES ('old-1', 'Old story', '', '', '[]',
            '2026-09-26T10:00:00+00:00', '2026-09-26T10:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    story_store.init_db()

    old = story_store.get_story("old-1")
    assert (old.status, old.position) == ("backlog", 0)


def test_stories_get_sequential_keys():
    first = _create()
    second = _create()
    assert (first.key, second.key) == ("ST-1", "ST-2")
    assert story_store.get_story(second.id).key == "ST-2"


def test_deleted_story_numbers_are_never_reused():
    _create()
    last = _create()
    story_store.delete_story(last.id)
    story_store.init_db()  # a restart must not rewind the counter either
    assert _create().key == "ST-3"


def test_init_db_numbers_older_stories_by_creation_time(monkeypatch, tmp_path):
    db = tmp_path / "old.db"
    monkeypatch.setenv("STORIES_DB_PATH", str(db))
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE stories (id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
            acceptance_criteria TEXT NOT NULL, definition_of_ready TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'backlog', position REAL NOT NULL DEFAULT 0);
        INSERT INTO stories (id, title, description, acceptance_criteria, definition_of_ready, created_at, updated_at)
        VALUES ('newer', 'Newer', '', '', '[]', '2026-09-26T11:00:00+00:00', '2026-09-26T11:00:00+00:00'),
               ('older', 'Older', '', '', '[]', '2026-09-26T10:00:00+00:00', '2026-09-26T10:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    story_store.init_db()
    story_store.init_db()  # idempotent: numbers stay put

    assert story_store.get_story("older").key == "ST-1"
    assert story_store.get_story("newer").key == "ST-2"
    assert _create().key == "ST-3"


def test_blocked_reason_is_kept_only_in_the_blocked_column():
    story = _create()
    blocked = story_store.move_story(story.id, status="blocked", position=1, blocked_reason="Waiting for API keys")
    assert (blocked.status, blocked.blocked_reason) == ("blocked", "Waiting for API keys")
    assert blocked.updated_at == story.updated_at
    assert blocked.fingerprint == story.fingerprint

    unblocked = story_store.move_story(story.id, status="in_sprint", position=1, blocked_reason="ignored")
    assert (unblocked.status, unblocked.blocked_reason) == ("in_sprint", None)


def test_init_db_adds_blocked_reason_to_an_older_database(monkeypatch, tmp_path):
    db = tmp_path / "old.db"
    monkeypatch.setenv("STORIES_DB_PATH", str(db))
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE stories (id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
            acceptance_criteria TEXT NOT NULL, definition_of_ready TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'backlog', position REAL NOT NULL DEFAULT 0, number INTEGER);
        INSERT INTO stories (id, title, description, acceptance_criteria, definition_of_ready, created_at, updated_at, number)
        VALUES ('old-1', 'Old story', '', '', '[]', '2026-09-26T10:00:00+00:00', '2026-09-26T10:00:00+00:00', 1);
        """
    )
    conn.commit()
    conn.close()

    story_store.init_db()

    assert story_store.get_story("old-1").blocked_reason is None
    moved = story_store.move_story("old-1", status="blocked", position=1, blocked_reason="Waiting")
    assert moved.blocked_reason == "Waiting"


def test_init_db_moves_stories_from_before_boards_to_an_st_board(monkeypatch, tmp_path):
    db = tmp_path / "old.db"
    monkeypatch.setenv("STORIES_DB_PATH", str(db))
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE stories (id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
            acceptance_criteria TEXT NOT NULL, definition_of_ready TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'backlog', position REAL NOT NULL DEFAULT 0, number INTEGER,
            blocked_reason TEXT);
        CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
        CREATE UNIQUE INDEX stories_number ON stories(number);
        INSERT INTO stories (id, title, description, acceptance_criteria, definition_of_ready, created_at, updated_at, number)
        VALUES ('a', 'A', '', '', '[]', '2026-09-26T10:00:00+00:00', '2026-09-26T10:00:00+00:00', 1),
               ('b', 'B', '', '', '[]', '2026-09-26T11:00:00+00:00', '2026-09-26T11:00:00+00:00', 2);
        -- ST-3 was created and deleted before the upgrade.
        INSERT INTO counters VALUES ('story', 3);
        """
    )
    conn.commit()
    conn.close()

    story_store.init_db()
    story_store.init_db()  # idempotent

    [board] = story_store.list_boards()
    assert (board.name, board.key_prefix, board.story_count) == ("Stories", "ST", 2)
    assert story_store.get_story("a").key == "ST-1"
    assert story_store.get_story("b").board_id == board.id
    assert _create().key == "ST-4"
    # Numbers are unique per board, so another board starts again at 1.
    other = story_store.create_board(name="Ops", key_prefix="OPS")
    assert _create(other.id).key == "OPS-1"


def test_init_db_on_a_new_database_creates_no_board(monkeypatch, tmp_path):
    monkeypatch.setenv("STORIES_DB_PATH", str(tmp_path / "new.db"))
    story_store.init_db()
    assert story_store.list_boards() == []


def test_each_board_numbers_its_own_stories_and_lists_only_them():
    flw = story_store.create_board(name="Flowershop Website", key_prefix="FLW")
    ops = story_store.create_board(name="Platform Maintenance", key_prefix="OPS")
    f1, f2, o1 = _create(flw.id), _create(flw.id), _create(ops.id)
    assert (f1.key, f2.key, o1.key) == ("FLW-1", "FLW-2", "OPS-1")
    assert [s.id for s, _ in story_store.list_stories(flw.id)] == [f1.id, f2.id]
    assert [s.id for s, _ in story_store.list_stories(ops.id)] == [o1.id]
    assert {b.key_prefix: b.story_count for b in story_store.list_boards()} == {"ST": 0, "FLW": 2, "OPS": 1}


def test_list_stories_only_reads_assessments_of_that_board():
    ops = story_store.create_board(name="Ops", key_prefix="OPS")
    story_store.add_assessment(_create(ops.id), _report(0.5))
    [(_, recent)] = story_store.list_stories(ops.id)
    assert [a.quality for a in recent] == [0.5]


def test_new_stories_go_to_the_end_of_their_own_boards_backlog():
    ops = story_store.create_board(name="Ops", key_prefix="OPS")
    for _ in range(3):
        _create()
    assert _create(ops.id).position == 1


def test_board_holds_at_most_max_stories_counting_every_column(monkeypatch):
    monkeypatch.setattr(story_store, "MAX_STORIES_PER_BOARD", 3)
    board = _st_board()
    first = _create()
    story_store.move_story(first.id, status="done", position=1)
    _create()
    _create()
    with pytest.raises(story_store.BoardFullError):
        _create()
    assert story_store.get_board(board.id).story_count == 3
    # The refused create hands out no number.
    story_store.delete_story(first.id)
    assert _create().key == "ST-4"


def test_create_story_on_unknown_board_raises():
    with pytest.raises(story_store.BoardNotFoundError):
        story_store.create_story("nope", **_STORY)


def test_key_prefix_is_unique_among_boards():
    with pytest.raises(story_store.DuplicateKeyPrefixError):
        story_store.create_board(name="Another", key_prefix="ST")


def test_update_board_changes_name_and_description_only():
    board = _st_board()
    _create()
    updated = story_store.update_board(board.id, name="Renamed", description="Goal")
    assert (updated.name, updated.description, updated.key_prefix, updated.story_count) == ("Renamed", "Goal", "ST", 1)
    assert story_store.update_board("nope", name="x", description="") is None


def test_delete_board_removes_its_stories_and_assessments_only():
    ops = story_store.create_board(name="Ops", key_prefix="OPS")
    doomed = _create(ops.id)
    story_store.add_assessment(doomed, _report(0.5))
    kept = _create()
    assert story_store.delete_board(ops.id) is True
    assert story_store.get_board(ops.id) is None
    assert story_store.get_story(doomed.id) is None
    assert story_store.list_assessments(doomed.id) == []
    assert story_store.get_story(kept.id) is not None
    assert story_store.delete_board(ops.id) is False


def test_recreated_prefix_does_not_reuse_story_numbers():
    ops = story_store.create_board(name="Ops", key_prefix="OPS")
    _create(ops.id)
    _create(ops.id)
    story_store.delete_board(ops.id)
    again = story_store.create_board(name="Ops again", key_prefix="OPS")
    assert _create(again.id).key == "OPS-3"
