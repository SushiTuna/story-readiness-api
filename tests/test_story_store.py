"""SQLite story store. conftest.py points STORIES_DB_PATH at a fresh file per test."""
from __future__ import annotations

import sqlite3

import pytest

from app import story_store
from app.schemas import CheckKind, CheckOut, VerdictEnum
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


def _evidence(*files: str, ui_change: bool = False) -> dict:
    """Evidence for a move to Done, as DoneEvidenceIn dumps it: a unit report (the first file, or a link), the rest as UI
    evidence, and one commit."""
    reports = [{"kind": "unit", "file_id": files[0], "url": None, "caption": "3 passed"}] if files else [
        {"kind": "unit", "file_id": None, "url": "https://ci.example.com/runs/1", "caption": None}
    ]
    ui = [{"file_id": f, "url": None, "caption": None} for f in files[1:]]
    return {
        "test_reports": reports,
        "ui_change": ui_change,
        "ui_evidence": ui,
        "commits": [{"hash": "a53287d", "message": "Scaffold the monorepo"}],
    }


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


def _assessed(story: story_store.StoredStory, verdict: VerdictEnum) -> None:
    report = _report(0.5)
    report.verdict = verdict
    story_store.add_assessment(story, report)


@pytest.mark.parametrize("verdict", [VerdictEnum.discuss, VerdictEnum.needs_refinement])
def test_discuss_or_needs_refinement_stories_stay_in_backlog_or_refinement(verdict):
    story = _create()
    _assessed(story, verdict)
    for status in ("ready_for_sprint", "in_sprint", "done", "blocked"):
        with pytest.raises(story_store.ReadinessError, match="ST-1 .* can only move to Backlog or Refinement"):
            story_store.move_story(story.id, status=status, position=1, blocked_reason="Stuck", done_evidence=_evidence())
    assert story_store.get_story(story.id).status == "backlog"
    assert [e.action for e in story_store.list_activity(_st_board().id, story.id)].count("story_moved") == 0
    assert story_store.move_story(story.id, status="refinement", position=1).status == "refinement"
    assert story_store.move_story(story.id, status="backlog", position=1).status == "backlog"


def test_a_stale_discuss_verdict_still_restricts_until_assessed_again():
    story = _create()
    _assessed(story, VerdictEnum.discuss)
    edited = story_store.update_story(story.id, **{**_STORY, "title": "Export orders as CSV, refined"})
    with pytest.raises(story_store.ReadinessError):
        story_store.move_story(story.id, status="ready_for_sprint", position=1)
    _assessed(edited, VerdictEnum.ready)
    assert story_store.move_story(story.id, status="ready_for_sprint", position=1).status == "ready_for_sprint"


def test_a_restricted_story_already_in_another_column_can_reorder_there_or_go_back():
    story = _create()
    story_store.move_story(story.id, status="in_sprint", position=1)
    _assessed(story, VerdictEnum.needs_refinement)
    assert story_store.move_story(story.id, status="in_sprint", position=0.5).position == 0.5
    with pytest.raises(story_store.ReadinessError):
        story_store.move_story(story.id, status="ready_for_sprint", position=1)
    assert story_store.move_story(story.id, status="refinement", position=1).status == "refinement"


@pytest.mark.parametrize("verdict", [None, VerdictEnum.not_ready, VerdictEnum.ready])
def test_other_verdicts_move_anywhere(verdict):
    story = _create()
    if verdict is not None:
        _assessed(story, verdict)
    assert story_store.move_story(story.id, status="in_sprint", position=1).status == "in_sprint"


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
    story_store.move_story(first.id, status="done", position=1, done_evidence=_evidence())
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


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

_AGENT = story_store.Actor("agent", "claude-code", "Tightening the criteria")


def _log(story_id: str | None = None) -> list[story_store.ActivityEntry]:
    return story_store.list_activity(_st_board().id, story_id=story_id)


def test_every_write_is_logged_with_its_actor():
    board = _st_board()
    story = _create(actor=_AGENT)
    story_store.update_story(story.id, **{**_STORY, "title": "Export orders"})
    story_store.move_story(story.id, status="refinement", position=1, actor=_AGENT)
    story_store.add_assessment(story, _report(0.8), actor=_AGENT)

    entries = _log(story.id)
    assert [e.action for e in entries] == ["story_assessed", "story_moved", "story_edited", "story_created"]
    assert [(e.actor_kind, e.actor) for e in entries] == [
        ("agent", "claude-code"), ("agent", "claude-code"), ("user", "board"), ("agent", "claude-code"),
    ]
    assert entries[0].detail == {"verdict": "ready", "quality": 0.8}
    assert entries[1].detail == {"from": "backlog", "to": "refinement"}
    assert entries[3].note == "Tightening the criteria"
    assert entries[2].note is None
    assert all(e.story_key == "ST-1" and e.board_id == board.id for e in entries)


def test_blocked_reason_is_logged():
    story = _create()
    story_store.move_story(story.id, status="blocked", position=1, blocked_reason="Waiting on legal")
    story_store.move_story(story.id, status="backlog", position=1)
    moved_out, moved_in = _log(story.id)[:2]
    assert moved_in.detail == {"from": "backlog", "to": "blocked", "blocked_reason": "Waiting on legal"}
    assert moved_out.detail == {"from": "blocked", "to": "backlog"}


def test_edit_logs_only_changed_fields_and_skips_no_ops():
    story = _create()
    story_store.update_story(story.id, **_STORY)
    assert [e.action for e in _log(story.id)] == ["story_created"]
    story_store.update_story(story.id, **{**_STORY, "description": "New", "definition_of_ready": []})
    assert _log(story.id)[0].detail == {"fields": ["description", "definition_of_ready"]}


def test_board_update_logs_old_and_new_values_and_skips_no_ops():
    board = _st_board()
    story_store.update_board(board.id, name="Stories", description="")
    assert [e.action for e in _log()] == ["board_created"]
    story_store.update_board(board.id, name="Shop", description="", actor=_AGENT)
    [updated] = [e for e in _log() if e.action == "board_updated"]
    assert updated.detail == {"from": {"name": "Stories"}, "to": {"name": "Shop"}}
    assert updated.actor == "claude-code"


def test_deleted_story_keeps_its_log_under_its_key():
    story = _create(actor=_AGENT)
    assert story_store.delete_story(story.id)
    deleted, created = _log()[:2]
    assert (deleted.action, deleted.story_key, deleted.story_id) == ("story_deleted", "ST-1", None)
    assert (created.action, created.story_key, created.story_id) == ("story_created", "ST-1", None)


def test_agents_by_story_lists_distinct_agents_oldest_first():
    first, second = _create(), _create()
    other = story_store.Actor("agent", "copilot", "Reordering")
    for actor in (_AGENT, other, _AGENT):
        story_store.move_story(first.id, status="refinement", position=1, actor=actor)
    story_store.move_story(second.id, status="refinement", position=2)  # a board user, not an agent
    assert story_store.agents_by_story(_st_board().id) == {first.id: ["claude-code", "copilot"]}


def test_list_activity_filters_by_story_and_limits():
    first, second = _create(), _create()
    assert [e.story_id for e in _log(first.id)] == [first.id]
    assert [e.story_id for e in story_store.list_activity(_st_board().id, limit=1)] == [second.id]


def test_lookup_by_key_and_prefix():
    story = _create()
    assert story_store.get_story_by_key("ST-1").id == story.id
    assert story_store.get_story_by_key("st-1").id == story.id
    for bad in ("ST-2", "ST", "ST-x", "XX-1", ""):
        assert story_store.get_story_by_key(bad) is None
    assert story_store.get_board_by_prefix("st").id == _st_board().id
    assert story_store.get_board_by_prefix("NOPE") is None


def test_column_bounds():
    board = _st_board()
    assert story_store.column_bounds(board.id, "backlog") is None
    _create(), _create()
    assert story_store.column_bounds(board.id, "backlog") == (1, 2)


def test_entering_done_needs_evidence_and_logs_it():
    story = _create()
    with pytest.raises(story_store.EvidenceError, match="needs done_evidence"):
        story_store.move_story(story.id, status="done", position=1)
    assert story_store.get_story(story.id).status == "backlog"

    report = story_store.save_evidence_file(story.id, "junit.xml", b"<testsuite tests='3'/>")
    shot = story_store.save_evidence_file(story.id, "C:\\shots\\home.png", b"png")
    assert (report.content_type, report.size, shot.filename) == ("application/xml", 22, "home.png")
    story_store.move_story(story.id, status="done", position=1, done_evidence=_evidence(report.id, shot.id, ui_change=True))

    [moved] = [e for e in _log(story.id) if e.action == "story_moved"]
    assert moved.detail == {
        "from": "backlog",
        "to": "done",
        "evidence": {
            "test_reports": [{
                "kind": "unit", "caption": "3 passed",
                "file": {"id": report.id, "filename": "junit.xml", "content_type": "application/xml", "size": 22},
            }],
            "ui_change": True,
            "ui_evidence": [{"file": {"id": shot.id, "filename": "home.png", "content_type": "image/png", "size": 3}}],
            "commits": [{"hash": "a53287d", "message": "Scaffold the monorepo"}],
        },
    }
    assert story_store.get_evidence_file(report.id).attached


def test_reordering_within_done_needs_no_evidence_and_other_moves_take_none():
    story = _create()
    story_store.move_story(story.id, status="done", position=1, done_evidence=_evidence())
    assert story_store.move_story(story.id, status="done", position=0.5).position == 0.5
    with pytest.raises(story_store.EvidenceError, match="only accepted"):
        story_store.move_story(story.id, status="done", position=2, done_evidence=_evidence())
    story_store.move_story(story.id, status="in_sprint", position=1)
    # Coming back to Done needs new evidence.
    with pytest.raises(story_store.EvidenceError):
        story_store.move_story(story.id, status="done", position=1)


def test_evidence_files_must_belong_to_the_story_and_be_unused():
    story, other = _create(), _create()
    theirs = story_store.save_evidence_file(other.id, "report.html", b"<p>ok</p>")
    with pytest.raises(story_store.EvidenceError, match="not uploaded for this story"):
        story_store.move_story(story.id, status="done", position=1, done_evidence=_evidence(theirs.id))
    with pytest.raises(story_store.EvidenceError, match="not uploaded for this story"):
        story_store.move_story(story.id, status="done", position=1, done_evidence=_evidence("nope"))

    mine = story_store.save_evidence_file(story.id, "report.html", b"<p>ok</p>")
    with pytest.raises(story_store.EvidenceError, match="already used"):
        story_store.move_story(story.id, status="done", position=1, done_evidence=_evidence(mine.id, mine.id))
    # A refused move attaches nothing.
    assert not story_store.get_evidence_file(mine.id).attached
    story_store.move_story(story.id, status="done", position=1, done_evidence=_evidence(mine.id))
    story_store.move_story(story.id, status="in_sprint", position=1)
    with pytest.raises(story_store.EvidenceError, match="already used"):
        story_store.move_story(story.id, status="done", position=1, done_evidence=_evidence(mine.id))


def test_evidence_file_types_sizes_and_deletes(monkeypatch):
    story = _create()
    with pytest.raises(story_store.EvidenceFileTypeError):
        story_store.save_evidence_file(story.id, "run.sh", b"echo")
    monkeypatch.setattr(story_store, "MAX_EVIDENCE_BYTES", 4)
    with pytest.raises(story_store.EvidenceFileTooLargeError):
        story_store.save_evidence_file(story.id, "log.txt", b"12345")
    assert story_store.save_evidence_file("nope", "log.txt", b"1") is None

    unused = story_store.save_evidence_file(story.id, "log.txt", b"1")
    path = story_store.evidence_path(unused)
    assert path.read_bytes() == b"1"
    assert story_store.delete_unattached_evidence_file(unused.id)
    assert not path.exists() and story_store.get_evidence_file(unused.id) is None
    assert not story_store.delete_unattached_evidence_file(unused.id)

    used = story_store.save_evidence_file(story.id, "log.txt", b"1")
    story_store.move_story(story.id, status="done", position=1, done_evidence=_evidence(used.id))
    with pytest.raises(story_store.EvidenceFileAttachedError):
        story_store.delete_unattached_evidence_file(used.id)


def test_deleting_a_story_or_board_removes_its_evidence_files():
    story = _create()
    path = story_store.evidence_path(story_store.save_evidence_file(story.id, "log.txt", b"1"))
    story_store.delete_story(story.id)
    assert not path.parent.exists()

    board = story_store.create_board(name="Shop", key_prefix="SHP")
    other = _create(board.id)
    path = story_store.evidence_path(story_store.save_evidence_file(other.id, "log.txt", b"1"))
    story_store.delete_board(board.id)
    assert not path.parent.exists()


# ---------------------------------------------------------------------------
# Human-only stories
# ---------------------------------------------------------------------------

_BOT = story_store.Actor("agent", "planner-bot", "Tidy up")


def _story_log(story_id: str) -> list[tuple[str, str, str, dict]]:
    """The story's activity, oldest first: (actor_kind, actor, action, detail)."""
    entries = story_store.list_activity(_st_board().id, story_id=story_id)
    return [(e.actor_kind, e.actor, e.action, e.detail) for e in reversed(entries)]


def test_agents_cannot_change_a_human_only_story_in_any_way():
    story = _create(human_only=True)
    before = story_store.get_story(story.id)
    attempts = {
        "update": lambda: story_store.update_story(story.id, **{**_STORY, "title": "Changed"}, actor=_BOT),
        "move": lambda: story_store.move_story(story.id, status="refinement", position=1, actor=_BOT),
        "assess": lambda: story_store.add_assessment(story, _report(0.9), actor=_BOT),
        "evidence": lambda: story_store.save_evidence_file(story.id, "report.txt", b"ok", actor=_BOT),
        "delete": lambda: story_store.delete_story(story.id, actor=_BOT),
    }
    for name, attempt in attempts.items():
        with pytest.raises(story_store.HumanOnlyError, match=r"^ST-1 is human-only: .*AI agents can't change it"):
            attempt()

    assert story_store.get_story(story.id) == before
    assert story_store.list_assessments(story.id) == []
    assert all(kind == "user" for kind, *_ in _story_log(story.id))


def test_people_on_the_board_can_still_change_a_human_only_story():
    story = _create(human_only=True)
    assert story_store.update_story(story.id, **{**_STORY, "title": "Changed"}).title == "Changed"
    assert story_store.move_story(story.id, status="refinement", position=1).status == "refinement"
    story_store.add_assessment(story_store.get_story(story.id), _report(0.9))
    assert story_store.save_evidence_file(story.id, "report.txt", b"ok") is not None
    assert story_store.delete_story(story.id) is True


def test_untagged_stories_are_unaffected_for_agents():
    story = _create()
    assert story_store.update_story(story.id, **{**_STORY, "title": "Changed"}, actor=_BOT).title == "Changed"
    assert story_store.move_story(story.id, status="refinement", position=1, actor=_BOT).status == "refinement"


def test_split_children_and_grandchildren_inherit_the_tag_and_log_it():
    parent = _create(title="Provision staging DB creds", human_only=True)
    child = _create(title="Create the DB user", parent_id=parent.id)
    grandchild = _create(title="Store the password in the vault", parent_id=child.id)

    for story, parent_key in ((child, "ST-1"), (grandchild, "ST-2")):
        stored = story_store.get_story(story.id)
        assert (stored.human_only, stored.parent_id, stored.parent_key) == (True, story.parent_id, parent_key)
        assert _story_log(story.id) == [
            ("user", "board", "story_created", {"title": story.title, "parent_key": parent_key}),
            ("user", "board", "story_human_only", {"human_only": True, "inherited_from": parent_key}),
        ]


def test_clearing_the_parent_keeps_existing_children_tagged():
    parent = _create(human_only=True)
    child = _create(parent_id=parent.id)

    cleared = story_store.set_human_only(parent.id, False)

    assert cleared.human_only is False
    assert story_store.get_story(child.id).human_only is True
    # Inheritance is from the direct parent at creation: a new child of the cleared parent starts untagged.
    assert _create(parent_id=parent.id).human_only is False


def test_a_tagged_story_created_directly_logs_the_tag_without_inherited_from():
    story = _create(human_only=True)
    assert _story_log(story.id)[1] == ("user", "board", "story_human_only", {"human_only": True})


def test_set_human_only_logs_who_changed_it_and_skips_no_ops():
    story = _create()
    story_store.set_human_only(story.id, False)  # already untagged: not logged
    story_store.set_human_only(story.id, True)
    story_store.set_human_only(story.id, True)  # already tagged: not logged
    story_store.set_human_only(story.id, False)

    assert [(kind, actor, action, detail) for kind, actor, action, detail in _story_log(story.id)[1:]] == [
        ("user", "board", "story_human_only", {"human_only": True}),
        ("user", "board", "story_human_only", {"human_only": False}),
    ]
    assert story_store.set_human_only("nope", True) is None


def test_agents_cannot_set_or_clear_the_tag_or_split_a_human_only_story():
    tagged = _create(human_only=True)
    untagged = _create()
    with pytest.raises(story_store.HumanOnlyError, match="Only people on the board"):
        story_store.set_human_only(untagged.id, True, actor=_BOT)
    with pytest.raises(story_store.HumanOnlyError, match="Only people on the board"):
        story_store.set_human_only(tagged.id, False, actor=_BOT)
    with pytest.raises(story_store.HumanOnlyError, match="Only people on the board"):
        _create(human_only=True, actor=_BOT)
    with pytest.raises(story_store.HumanOnlyError, match="ST-1 is human-only"):
        _create(parent_id=tagged.id, actor=_BOT)

    assert story_store.get_story(tagged.id).human_only is True
    assert story_store.get_story(untagged.id).human_only is False
    assert len(story_store.list_stories(_st_board().id)) == 2  # the refused split created nothing


def test_agents_can_split_an_untagged_story():
    parent = _create()
    child = _create(parent_id=parent.id, actor=_BOT)
    assert (child.human_only, child.parent_key) == (False, "ST-1")


def test_a_parent_must_be_on_the_same_board():
    other = story_store.create_board(name="Other", key_prefix="OT")
    parent = _create(other.id)
    with pytest.raises(story_store.ParentNotFoundError):
        _create(parent_id=parent.id)
    with pytest.raises(story_store.ParentNotFoundError):
        _create(parent_id="nope")


def test_deleting_a_parent_keeps_the_child_and_its_tag():
    parent = _create(human_only=True)
    child = _create(parent_id=parent.id)
    story_store.delete_story(parent.id)
    stored = story_store.get_story(child.id)
    assert (stored.human_only, stored.parent_id, stored.parent_key) == (True, None, None)


def test_untagged_fingerprints_are_unchanged_and_tagging_makes_the_assessment_stale():
    from app.linear_client import issue_fingerprint

    story = _create()
    # The formula from before human-only existed: no untagged story may turn stale after the upgrade.
    assert story.fingerprint == issue_fingerprint(
        _STORY["title"], "\n".join([_STORY["description"], _STORY["acceptance_criteria"], *_STORY["definition_of_ready"]])
    )
    story_store.add_assessment(story, _report(0.9))
    fresh = lambda: story_store.list_assessments(story.id)[0].fingerprint == story_store.get_story(story.id).fingerprint  # noqa: E731

    assert fresh()
    story_store.set_human_only(story.id, True)
    assert not fresh()
    story_store.set_human_only(story.id, False)
    assert fresh()


def test_set_human_only_does_not_touch_updated_at():
    story = _create()
    assert story_store.set_human_only(story.id, True).updated_at == story.updated_at


def test_init_db_adds_human_only_parent_and_tags_to_an_older_database(monkeypatch, tmp_path):
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
        INSERT INTO stories (id, title, description, acceptance_criteria, definition_of_ready, created_at, updated_at, number)
        VALUES ('old-1', 'Old story', '', '', '[]', '2026-09-26T10:00:00+00:00', '2026-09-26T10:00:00+00:00', 1);
        """
    )
    conn.commit()
    conn.close()

    story_store.init_db()

    old = story_store.get_story("old-1")
    assert (old.human_only, old.parent_id, old.parent_key, old.tags) == (False, None, None, [])
    assert story_store.set_human_only("old-1", True).human_only is True


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

def test_create_stores_tags_and_logs_them():
    story = _create(tags=["backend", "security"])
    assert story_store.get_story(story.id).tags == ["backend", "security"]
    assert _story_log(story.id) == [
        ("user", "board", "story_created", {"title": _STORY["title"], "tags": ["backend", "security"]})
    ]
    untagged = _create()
    assert untagged.tags == [] and _story_log(untagged.id)[0][3] == {"title": _STORY["title"]}


def test_update_replaces_tags_or_keeps_them_when_left_out():
    story = _create(tags=["backend"])
    kept = story_store.update_story(story.id, **{**_STORY, "title": "Renamed"})
    assert kept.tags == ["backend"]
    replaced = story_store.update_story(story.id, **{**_STORY, "title": "Renamed"}, tags=["frontend", "design"])
    assert replaced.tags == ["frontend", "design"]
    assert story_store.update_story(story.id, **{**_STORY, "title": "Renamed"}, tags=[]).tags == []
    assert [detail for _, _, action, detail in _story_log(story.id) if action == "story_edited"] == [
        {"fields": ["title"]},
        {"fields": ["tags"]},
        {"fields": ["tags"]},
    ]


def test_saving_the_same_tags_is_not_logged():
    story = _create(tags=["backend"])
    story_store.update_story(story.id, **_STORY, tags=["backend"])
    assert [action for _, _, action, _ in _story_log(story.id)] == ["story_created"]


def test_changing_only_tags_does_not_make_the_assessment_stale():
    story = _create()
    assessment = story_store.add_assessment(story, _report(0.9))
    retagged = story_store.update_story(story.id, **_STORY, tags=["platform"])
    assert retagged.fingerprint == assessment.fingerprint


def test_agents_can_tag_stories_but_not_human_only_ones():
    story = _create()
    assert story_store.update_story(story.id, **_STORY, tags=["backend"], actor=_BOT).tags == ["backend"]
    assert _story_log(story.id)[-1] == ("agent", "planner-bot", "story_edited", {"fields": ["tags"]})

    locked = _create(human_only=True)
    with pytest.raises(story_store.HumanOnlyError):
        story_store.update_story(locked.id, **_STORY, tags=["backend"], actor=_BOT)
    assert story_store.get_story(locked.id).tags == []


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _refs(refs: list[story_store.StoryRef]) -> list[tuple[str, str]]:
    return [(r.key, r.status) for r in refs]


def test_add_blocker_links_both_stories_and_logs_it_once():
    api = _create(title="Orders API")
    ui = _create(title="Orders page")

    story = story_store.add_blocker(ui.id, api.id)
    story_store.add_blocker(ui.id, api.id)  # already there: not logged again

    assert _refs(story.blocked_by) == [("ST-1", "backlog")]
    assert story.blocked_by[0].title == "Orders API"
    assert _refs(story_store.get_story(api.id).blocks) == [("ST-2", "backlog")]
    assert _refs(story_store.get_story_by_key("ST-2").blocked_by) == [("ST-1", "backlog")]
    listed = {s.key: s for s, _ in story_store.list_stories(_st_board().id)}
    assert (_refs(listed["ST-2"].blocked_by), _refs(listed["ST-1"].blocks)) == ([("ST-1", "backlog")], [("ST-2", "backlog")])
    assert [a for _, _, a, _ in _story_log(ui.id)].count("story_blocker_added") == 1
    assert _story_log(ui.id)[-1] == ("user", "board", "story_blocker_added", {"blocker_key": "ST-1"})
    # Not an edit: the text and its assessment are untouched.
    assert story_store.get_story(ui.id).updated_at == ui.updated_at


def test_a_waiting_story_cannot_enter_in_sprint_or_done_until_its_blockers_are_done():
    api = _create(title="Orders API")
    db = _create(title="Orders table")
    ui = _create(title="Orders page")
    story_store.add_blocker(ui.id, db.id)
    story_store.add_blocker(ui.id, api.id)

    for status in ("in_sprint", "done"):
        with pytest.raises(
            story_store.BlockersOpenError,
            match=r"^ST-3 waits on ST-1 and ST-2, which are not done yet\. It can enter In sprint or Done once they are",
        ):
            story_store.move_story(ui.id, status=status, position=1, done_evidence=_evidence() if status == "done" else None)
    # It can still be planned, refined or parked.
    for status in ("refinement", "ready_for_sprint", "blocked", "backlog"):
        assert story_store.move_story(ui.id, status=status, position=1, blocked_reason="x").status == status

    story_store.move_story(api.id, status="done", position=1, done_evidence=_evidence())
    with pytest.raises(story_store.BlockersOpenError, match=r"^ST-3 waits on ST-2, which is not done yet\. .* once it is"):
        story_store.move_story(ui.id, status="in_sprint", position=1)
    story_store.move_story(db.id, status="done", position=2, done_evidence=_evidence())
    assert story_store.move_story(ui.id, status="in_sprint", position=1).status == "in_sprint"
    assert _refs(story_store.get_story(ui.id).blocked_by) == [("ST-1", "done"), ("ST-2", "done")]


def test_a_story_already_in_sprint_can_be_reordered_but_not_finished_while_it_waits():
    api = _create()
    ui = _create()
    story_store.move_story(ui.id, status="in_sprint", position=1)
    story_store.add_blocker(ui.id, api.id)  # recorded even though ST-2 is already in the sprint

    assert story_store.move_story(ui.id, status="in_sprint", position=0.5).position == 0.5
    with pytest.raises(story_store.BlockersOpenError):
        story_store.move_story(ui.id, status="done", position=1, done_evidence=_evidence())
    assert story_store.get_story(ui.id).status == "in_sprint"


def test_a_story_cannot_wait_on_itself_directly_or_through_others():
    a, b, c = _create(), _create(), _create()
    with pytest.raises(story_store.DependencyCycleError, match="^ST-1 can't wait on itself"):
        story_store.add_blocker(a.id, a.id)
    story_store.add_blocker(b.id, a.id)  # ST-2 waits on ST-1
    story_store.add_blocker(c.id, b.id)  # ST-3 waits on ST-2
    with pytest.raises(story_store.DependencyCycleError, match="^ST-2 already waits on ST-1"):
        story_store.add_blocker(a.id, b.id)
    with pytest.raises(story_store.DependencyCycleError, match="^ST-3 already waits on ST-1, directly or through"):
        story_store.add_blocker(a.id, c.id)
    assert story_store.get_story(a.id).blocked_by == []
    # A diamond is not a cycle.
    story_store.add_blocker(c.id, a.id)
    assert _refs(story_store.get_story(c.id).blocked_by) == [("ST-1", "backlog"), ("ST-2", "backlog")]


def test_a_blocker_must_be_on_the_same_board():
    other = story_store.create_board(name="Other", key_prefix="OT")
    elsewhere = _create(other.id)
    story = _create()
    with pytest.raises(story_store.BlockerNotFoundError):
        story_store.add_blocker(story.id, elsewhere.id)
    with pytest.raises(story_store.BlockerNotFoundError):
        story_store.add_blocker(story.id, "nope")
    assert story_store.add_blocker("nope", story.id) is None


def test_remove_blocker_logs_it_and_skips_no_ops():
    api, ui = _create(), _create()
    story_store.add_blocker(ui.id, api.id)

    story = story_store.remove_blocker(ui.id, api.id)
    story_store.remove_blocker(ui.id, api.id)  # already gone: not logged

    assert story.blocked_by == [] and story_store.get_story(api.id).blocks == []
    assert [a for _, _, a, _ in _story_log(ui.id)] == ["story_created", "story_blocker_added", "story_blocker_removed"]
    assert _story_log(ui.id)[-1][3] == {"blocker_key": "ST-1"}
    assert story_store.move_story(ui.id, status="in_sprint", position=1).status == "in_sprint"
    assert story_store.remove_blocker("nope", api.id) is None


def test_deleting_a_blocker_lifts_the_dependency():
    api, ui = _create(), _create()
    story_store.add_blocker(ui.id, api.id)
    story_store.delete_story(api.id)
    assert story_store.get_story(ui.id).blocked_by == []
    assert story_store.move_story(ui.id, status="in_sprint", position=1).status == "in_sprint"


def test_agents_manage_dependencies_except_on_human_only_stories():
    api, ui = _create(), _create()
    tagged = _create(human_only=True)
    creds = _create()

    assert _refs(story_store.add_blocker(ui.id, api.id, actor=_BOT).blocked_by) == [("ST-1", "backlog")]
    assert _story_log(ui.id)[-1] == ("agent", "planner-bot", "story_blocker_added", {"blocker_key": "ST-1"})
    # Waiting on a human-only story changes only the waiting one.
    story_store.add_blocker(api.id, tagged.id, actor=_BOT)
    with pytest.raises(story_store.HumanOnlyError, match="^ST-3 is human-only"):
        story_store.add_blocker(tagged.id, creds.id, actor=_BOT)
    story_store.add_blocker(tagged.id, creds.id)  # someone on the board can
    with pytest.raises(story_store.HumanOnlyError):
        story_store.remove_blocker(tagged.id, creds.id, actor=_BOT)
    assert _refs(story_store.get_story(tagged.id).blocked_by) == [("ST-4", "backlog")]
