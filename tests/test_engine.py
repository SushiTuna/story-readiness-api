"""Tests for app.engine: assess() verdict logic, pre-processing, and DoR normalisation."""

from __future__ import annotations

import pytest
import typesafe_sdk as ts

from app.engine import assess, extract_ac, normalise_dor
from app.schemas import AssessRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _noul(noul: float) -> ts.NoulAnswer:
    return ts.NoulAnswer(noul=noul)


def _score(score: int, confidence: float = 0.9, levels: int = 3) -> ts.ScoreAnswer:
    legend = {i: f"level_{i}" for i in range(levels)}
    probs = {i: (1.0 if i == score else 0.0) for i in range(levels)}
    return ts.ScoreAnswer(score=score, confidence=confidence, legend=legend, probabilities=probs)


def _choice(choice: str, confidence: float = 0.9) -> ts.ChoiceAnswer:
    return ts.ChoiceAnswer(choice=choice, confidence=confidence, probabilities={choice: confidence})


def _jev_response(answers: dict) -> object:
    """Return a minimal JevResponse-compatible object."""
    from app.jev_client import JevResponse
    return JevResponse(
        model="jev-1",
        request_id=None,
        input_tokens=100,
        latency_ms=200,
        answers=answers,
    )


def _all_pass_answers(story_type: str = "user_feature") -> dict:
    """Return answers that make every applicable check pass for *story_type*."""
    return {
        "story_type":       _choice(story_type),
        "ac_quality":       _score(2, confidence=0.9),    # max score → value=1.0
        "has_persona":      _noul(1.0),
        "value_statement":  _noul(1.0),
        "failure_handling": _noul(1.0),
        "safe_rollout":     _noul(1.0),
        "title_clarity":    _score(2, confidence=0.9),    # max score → value=1.0
        "scope_size":       _noul(0.0),
    }


def _make_request(
    title: str = "Fix login button",
    description: str = "The login button is broken.",
    acceptance_criteria: str = "AC: it works",
    definition_of_ready: list[str] | None = None,
) -> AssessRequest:
    return AssessRequest(
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
        definition_of_ready=definition_of_ready or [],
    )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_checks_pass_gives_ready(monkeypatch):
    """All checks pass → verdict 'ready'."""
    answers = _all_pass_answers("user_feature")

    async def mock_call_jev(state, questions):
        return _jev_response(answers)

    monkeypatch.setattr("app.engine.call_jev", mock_call_jev)

    req = _make_request()
    report = await assess(req, source="paste")

    assert report.verdict == "ready"
    assert report.quality >= 0.6
    assert report.blockers_failed == []


@pytest.mark.asyncio
async def test_empty_ac_gives_not_ready(monkeypatch):
    """ac_present fails when acceptance_criteria is empty → not_ready."""
    answers = _all_pass_answers("user_feature")

    async def mock_call_jev(state, questions):
        return _jev_response(answers)

    monkeypatch.setattr("app.engine.call_jev", mock_call_jev)

    req = _make_request(acceptance_criteria="")
    report = await assess(req, source="paste")

    assert report.verdict == "not_ready"
    assert "ac_present" in report.blockers_failed


@pytest.mark.asyncio
async def test_value_statement_not_checked_for_bugs(monkeypatch):
    """Finding 4: value_statement must not appear in checks for bug stories (spec says not required)."""
    answers = _all_pass_answers("bug")

    async def mock_call_jev(state, questions):
        return _jev_response(answers)

    monkeypatch.setattr("app.engine.call_jev", mock_call_jev)

    req = _make_request()
    report = await assess(req, source="paste")

    check_ids = [c.id for c in report.checks]
    assert "value_statement" not in check_ids


@pytest.mark.asyncio
async def test_has_persona_fail_on_bug_is_not_blocker(monkeypatch):
    """has_persona is only applicable to user_feature → failing it on a bug is ignored."""
    answers = _all_pass_answers("bug")
    answers["has_persona"] = _noul(0.1)  # confidently fails

    async def mock_call_jev(state, questions):
        return _jev_response(answers)

    monkeypatch.setattr("app.engine.call_jev", mock_call_jev)

    req = _make_request()
    report = await assess(req, source="paste")

    # has_persona should not appear in checks for a bug
    check_ids = [c.id for c in report.checks]
    assert "has_persona" not in check_ids
    assert report.verdict != "not_ready" or "has_persona" not in report.blockers_failed


@pytest.mark.asyncio
async def test_has_persona_fail_on_user_feature_gives_not_ready(monkeypatch):
    """has_persona fails confidently on user_feature → not_ready."""
    answers = _all_pass_answers("user_feature")
    answers["has_persona"] = _noul(0.1)  # confidently fails, not unsure

    async def mock_call_jev(state, questions):
        return _jev_response(answers)

    monkeypatch.setattr("app.engine.call_jev", mock_call_jev)

    req = _make_request()
    report = await assess(req, source="paste")

    assert report.verdict == "not_ready"
    assert "has_persona" in report.blockers_failed


@pytest.mark.asyncio
async def test_low_quality_gives_needs_refinement(monkeypatch):
    """All weighted checks pass but quality < 0.6 → needs_refinement."""
    answers = _all_pass_answers("user_feature")
    # ac_quality: score=0 → value=0/2=0.0
    answers["ac_quality"] = _score(0, confidence=0.9)
    # value_statement, failure_handling, title_clarity all low
    answers["value_statement"] = _noul(0.1)
    answers["failure_handling"] = _noul(0.1)
    answers["title_clarity"] = _score(0, confidence=0.9)

    async def mock_call_jev(state, questions):
        return _jev_response(answers)

    monkeypatch.setattr("app.engine.call_jev", mock_call_jev)

    req = _make_request()
    report = await assess(req, source="paste")

    assert report.quality < 0.6
    assert report.verdict == "needs_refinement"


@pytest.mark.asyncio
async def test_unsure_answer_gives_discuss(monkeypatch):
    """An unsure noul answer (0.5) → discuss."""
    answers = _all_pass_answers("user_feature")
    answers["value_statement"] = _noul(0.5)  # exactly in unsure zone [0.35, 0.65]

    async def mock_call_jev(state, questions):
        return _jev_response(answers)

    monkeypatch.setattr("app.engine.call_jev", mock_call_jev)

    req = _make_request()
    report = await assess(req, source="paste")

    assert report.verdict == "discuss"
    assert "value_statement" in report.to_discuss


@pytest.mark.asyncio
async def test_dor_items_are_normalised(monkeypatch):
    """Blank items stripped, duplicates removed, capped at 12."""
    raw_dor = (
        ["  ", "Item A", "Item A", "Item B"]  # blank + duplicate
        + [f"Item {i}" for i in range(15)]     # 15 extra → total > 12
    )

    seen_questions: dict = {}

    async def mock_call_jev(state, questions):
        seen_questions.update(questions)
        ans = _all_pass_answers("technical")
        # Add dor answers for whatever dor_N keys were sent
        for k in questions:
            if k.startswith("dor_"):
                ans[k] = _noul(1.0)
        return _jev_response(ans)

    monkeypatch.setattr("app.engine.call_jev", mock_call_jev)

    req = AssessRequest(
        title="Tech story",
        description="desc",
        acceptance_criteria="AC present",
        definition_of_ready=raw_dor,
    )
    report = await assess(req, source="paste")

    dor_check_ids = [c.id for c in report.checks if c.id.startswith("dor_")]
    # After normalisation: blank stripped (1), duplicate stripped (1), capped at 12
    assert len(dor_check_ids) == 12


@pytest.mark.asyncio
async def test_ac_extracted_from_description(monkeypatch):
    """AC section embedded in description is extracted when acceptance_criteria is empty."""
    description = (
        "This story is about improving performance.\n\n"
        "## Acceptance Criteria\n"
        "- Response time < 200ms\n"
        "- No errors in logs\n"
    )

    captured_state: dict = {}

    async def mock_call_jev(state, questions):
        captured_state.update(state)
        ans = _all_pass_answers("technical")
        return _jev_response(ans)

    monkeypatch.setattr("app.engine.call_jev", mock_call_jev)

    req = _make_request(description=description, acceptance_criteria="")
    report = await assess(req, source="paste")

    # The AC should have been extracted — description no longer contains the AC block
    assert "Acceptance Criteria" not in captured_state["description"]
    assert "200ms" in captured_state["acceptance_criteria"]
    # ac_present should pass since we extracted non-empty AC
    ac_check = next((c for c in report.checks if c.id == "ac_present"), None)
    assert ac_check is not None
    assert ac_check.passed is True


@pytest.mark.asyncio
async def test_story_type_populated_from_jev(monkeypatch):
    """story_type.choice and story_type.confidence come from Jev's ChoiceAnswer."""
    answers = _all_pass_answers("technical")
    answers["story_type"] = _choice("technical", confidence=0.85)

    async def mock_call_jev(state, questions):
        return _jev_response(answers)

    monkeypatch.setattr("app.engine.call_jev", mock_call_jev)

    req = _make_request()
    report = await assess(req, source="paste")

    assert report.story_type.choice == "technical"
    assert abs(report.story_type.confidence - 0.85) < 1e-9


@pytest.mark.asyncio
async def test_flag_check_unsure_does_not_give_discuss(monkeypatch):
    """Bug fix: an unsure flag check must not change the verdict to 'discuss'."""
    answers = _all_pass_answers("technical")
    # scope_size is a flag — noul=0.5 puts it in the unsure band
    answers["scope_size"] = _noul(0.5)

    async def mock_call_jev(state, questions):
        return _jev_response(answers)

    monkeypatch.setattr("app.engine.call_jev", mock_call_jev)

    req = _make_request()
    report = await assess(req, source="paste")

    # scope_size unsure must appear in checks but NOT in to_discuss and NOT change verdict
    scope = next(c for c in report.checks if c.id == "scope_size")
    assert scope.unsure is True          # the flag itself is correctly marked unsure
    assert "scope_size" not in report.to_discuss  # but must not influence verdict
    assert report.verdict == "ready"     # everything else passes → ready


@pytest.mark.asyncio
async def test_scope_size_value_not_inverted(monkeypatch):
    """Bug fix: scope_size value=1.0 means well-scoped (good), value=0.0 means oversized (bad)."""
    answers = _all_pass_answers("user_feature")
    # Jev says story is well-sized (noul → "yes, appropriately sized") → high value
    answers["scope_size"] = _noul(0.95)

    async def mock_call_jev(state, questions):
        return _jev_response(answers)

    monkeypatch.setattr("app.engine.call_jev", mock_call_jev)

    req = _make_request()
    report = await assess(req, source="paste")

    scope = next(c for c in report.checks if c.id == "scope_size")
    assert scope.value >= 0.9   # high value = good (well-scoped)
    assert scope.passed is True


@pytest.mark.asyncio
async def test_failing_check_has_ask_questions(monkeypatch):
    """A check that fails must carry a non-empty ask list with an author-facing question."""
    answers = _all_pass_answers("user_feature")
    answers["has_persona"] = _noul(0.1)  # confidently fails

    async def mock_call_jev(state, questions):
        return _jev_response(answers)

    monkeypatch.setattr("app.engine.call_jev", mock_call_jev)

    req = _make_request()
    report = await assess(req, source="paste")

    persona_check = next(c for c in report.checks if c.id == "has_persona")
    assert persona_check.passed is False
    assert len(persona_check.ask) > 0
    assert isinstance(persona_check.ask[0], str)


@pytest.mark.asyncio
async def test_unsure_check_has_ask_questions(monkeypatch):
    """A check that is unsure must carry a non-empty ask list."""
    answers = _all_pass_answers("user_feature")
    answers["value_statement"] = _noul(0.5)  # in unsure band [0.35, 0.65]

    async def mock_call_jev(state, questions):
        return _jev_response(answers)

    monkeypatch.setattr("app.engine.call_jev", mock_call_jev)

    req = _make_request()
    report = await assess(req, source="paste")

    vs_check = next(c for c in report.checks if c.id == "value_statement")
    assert vs_check.unsure is True
    assert len(vs_check.ask) > 0


@pytest.mark.asyncio
async def test_passing_check_has_empty_ask(monkeypatch):
    """A check that passes confidently must have an empty ask list."""
    answers = _all_pass_answers("user_feature")

    async def mock_call_jev(state, questions):
        return _jev_response(answers)

    monkeypatch.setattr("app.engine.call_jev", mock_call_jev)

    req = _make_request()
    report = await assess(req, source="paste")

    for chk in report.checks:
        if chk.passed and not chk.unsure:
            assert chk.ask == [], f"check {chk.id!r} should have empty ask but got {chk.ask!r}"


@pytest.mark.asyncio
async def test_ac_present_failure_has_ask_question(monkeypatch):
    """ac_present failing must populate ask so the author knows what is needed."""
    answers = _all_pass_answers("user_feature")

    async def mock_call_jev(state, questions):
        return _jev_response(answers)

    monkeypatch.setattr("app.engine.call_jev", mock_call_jev)

    req = _make_request(acceptance_criteria="")
    report = await assess(req, source="paste")

    ac_check = next(c for c in report.checks if c.id == "ac_present")
    assert ac_check.passed is False
    assert len(ac_check.ask) > 0


@pytest.mark.asyncio
async def test_dor_failing_check_has_ask(monkeypatch):
    """A DoR check that fails must carry a confirmation question in ask."""
    dor_item = "The API contract must be reviewed by the team"

    async def mock_call_jev(state, questions):
        ans = _all_pass_answers("technical")
        ans["dor_0"] = _noul(0.1)  # fails
        return _jev_response(ans)

    monkeypatch.setattr("app.engine.call_jev", mock_call_jev)

    req = AssessRequest(
        title="Tech story",
        description="desc",
        acceptance_criteria="AC present",
        definition_of_ready=[dor_item],
    )
    report = await assess(req, source="paste")

    dor_check = next(c for c in report.checks if c.id == "dor_0")
    assert dor_check.passed is False
    assert len(dor_check.ask) > 0
    assert dor_item[:50] in dor_check.ask[0]


# ---------------------------------------------------------------------------
# Unit tests for helpers (no async needed)
# ---------------------------------------------------------------------------

def test_extract_ac_leaves_existing_ac_unchanged():
    desc = "Some description"
    ac = "Existing AC"
    out_desc, out_ac = extract_ac(desc, ac)
    assert out_desc == desc
    assert out_ac == ac


def test_extract_ac_splits_markdown_heading():
    desc = "Story body.\n\n## Acceptance Criteria\n- Item one\n- Item two\n"
    out_desc, out_ac = extract_ac(desc, "")
    assert "Acceptance Criteria" not in out_desc
    assert "Item one" in out_ac


def test_extract_ac_no_heading_returns_original():
    desc = "Story without any AC section."
    out_desc, out_ac = extract_ac(desc, "")
    assert out_desc == desc
    assert out_ac == ""


def test_normalise_dor_strips_blank_deduplicates_caps():
    items = ["", "  ", "A", "A", "B"] + [str(i) for i in range(20)]
    result = normalise_dor(items)
    assert "" not in result
    assert result.count("A") == 1
    assert len(result) == 12


def test_normalise_dor_truncates_long_items():
    long_item = "x" * 500
    result = normalise_dor([long_item])
    assert len(result[0]) == 300
