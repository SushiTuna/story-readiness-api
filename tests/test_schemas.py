"""Smoke tests for src/app/schemas.py — instantiate, dump, round-trip."""
import pytest

from app.schemas import (
    AssessRequest,
    CheckKind,
    CheckOut,
    ErrorOut,
    ImportRequest,
    ReportOut,
    SourceOut,
    StoryOut,
    StorySource,
    StoryType,
    StoryTypeChoice,
    VerdictEnum,
)


# ---------------------------------------------------------------------------
# Enum sanity
# ---------------------------------------------------------------------------

def test_story_type_choice_values():
    assert StoryTypeChoice.user_feature == "user_feature"
    assert StoryTypeChoice.technical == "technical"
    assert StoryTypeChoice.bug == "bug"


def test_story_source_values():
    assert StorySource.paste == "paste"
    assert StorySource.jira == "jira"
    assert StorySource.linear == "linear"


def test_check_kind_values():
    assert CheckKind.code == "code"
    assert CheckKind.blocker == "blocker"
    assert CheckKind.weighted == "weighted"
    assert CheckKind.flag == "flag"


def test_verdict_enum_values():
    assert VerdictEnum.ready == "ready"
    assert VerdictEnum.discuss == "discuss"
    assert VerdictEnum.needs_refinement == "needs_refinement"
    assert VerdictEnum.not_ready == "not_ready"


# ---------------------------------------------------------------------------
# AssessRequest
# ---------------------------------------------------------------------------

def test_assess_request_minimal():
    req = AssessRequest(title="Fix login bug")
    assert req.title == "Fix login bug"
    assert req.description == ""
    assert req.acceptance_criteria == ""
    assert req.definition_of_ready == []


def test_assess_request_full_round_trip():
    req = AssessRequest(
        title="Add dark mode",
        description="As a user I want dark mode.",
        acceptance_criteria="- Toggle exists",
        definition_of_ready=["No open bugs"],
    )
    data = req.model_dump()
    req2 = AssessRequest(**data)
    assert req2 == req


def test_assess_request_title_too_short():
    with pytest.raises(Exception):
        AssessRequest(title="")


def test_assess_request_title_too_long():
    with pytest.raises(Exception):
        AssessRequest(title="x" * 501)


# ---------------------------------------------------------------------------
# ImportRequest
# ---------------------------------------------------------------------------

def test_import_request_minimal():
    req = ImportRequest(key="PROJ-123")
    assert req.key == "PROJ-123"
    assert req.definition_of_ready == []


def test_import_request_round_trip():
    req = ImportRequest(key="ENG-42", definition_of_ready=["Item 1"])
    data = req.model_dump()
    assert ImportRequest(**data) == req


def test_import_request_key_too_long():
    with pytest.raises(Exception):
        ImportRequest(key="x" * 101)


# ---------------------------------------------------------------------------
# StoryOut
# ---------------------------------------------------------------------------

def test_story_out_instantiation():
    story = StoryOut(
        title="My story",
        description="Desc",
        acceptance_criteria="- AC1",
        key=None,
        source=StorySource.paste,
        url=None,
    )
    assert story.source == "paste"
    assert story.key is None


def test_story_out_round_trip():
    story = StoryOut(
        title="My story",
        description="Desc",
        acceptance_criteria="- AC1",
        key="PROJ-1",
        source="jira",
        url="https://example.atlassian.net/browse/PROJ-1",
    )
    data = story.model_dump()
    assert StoryOut(**data) == story


# ---------------------------------------------------------------------------
# StoryType
# ---------------------------------------------------------------------------

def test_story_type_instantiation():
    st = StoryType(
        choice=StoryTypeChoice.user_feature,
        confidence=0.9,
        probabilities={"user_feature": 0.9, "technical": 0.07, "bug": 0.03},
    )
    assert st.choice == "user_feature"
    assert st.confidence == 0.9


def test_story_type_confidence_bounds():
    with pytest.raises(Exception):
        StoryType(choice="bug", confidence=1.1, probabilities={})
    with pytest.raises(Exception):
        StoryType(choice="bug", confidence=-0.1, probabilities={})


# ---------------------------------------------------------------------------
# CheckOut
# ---------------------------------------------------------------------------

def _minimal_check(**overrides) -> CheckOut:
    defaults = dict(
        id="ac_present",
        label="AC present",
        kind=CheckKind.blocker,
        value=1.0,
        passed=True,
        unsure=False,
        answer={"yes": 0.95},
        ask=[],
    )
    defaults.update(overrides)
    return CheckOut(**defaults)


def test_check_out_instantiation():
    c = _minimal_check()
    assert c.id == "ac_present"
    assert c.passed is True


def test_check_out_value_bounds():
    with pytest.raises(Exception):
        _minimal_check(value=1.1)
    with pytest.raises(Exception):
        _minimal_check(value=-0.01)


def test_check_out_round_trip():
    c = _minimal_check(answer={"score": 0.8, "max": 1, "label": "good"})
    data = c.model_dump()
    assert CheckOut(**data) == c


# ---------------------------------------------------------------------------
# ReportOut
# ---------------------------------------------------------------------------

def _minimal_report(**overrides) -> ReportOut:
    story = StoryOut(
        title="T",
        description="D",
        acceptance_criteria="A",
        key=None,
        source="paste",
        url=None,
    )
    story_type = StoryType(
        choice="user_feature",
        confidence=0.8,
        probabilities={"user_feature": 0.8, "technical": 0.1, "bug": 0.1},
    )
    defaults = dict(
        verdict=VerdictEnum.ready,
        quality=0.85,
        checks=[],
        story=story,
        story_type=story_type,
        blockers_failed=[],
        to_discuss=[],
        jev_model="jev-1.13.0",
        request_id="req-abc",
        latency_ms=350,
        input_tokens=120,
    )
    defaults.update(overrides)
    return ReportOut(**defaults)


def test_report_out_instantiation():
    r = _minimal_report()
    assert r.verdict == "ready"
    assert r.jev_model == "jev-1.13.0"


def test_report_out_alias_serialization():
    """by_alias=True must produce key 'model', not 'jev_model'."""
    r = _minimal_report()
    data = r.model_dump(by_alias=True)
    assert "model" in data
    assert "jev_model" not in data
    assert data["model"] == "jev-1.13.0"


def test_report_out_default_dump_has_jev_model():
    """Without by_alias the internal field name is jev_model."""
    r = _minimal_report()
    data = r.model_dump()
    assert "jev_model" in data


def test_report_out_populate_by_alias():
    """populate_by_name=True — can construct using the alias 'model'."""
    r = ReportOut(
        verdict="not_ready",
        quality=0.4,
        checks=[],
        story=StoryOut(
            title="T", description="D", acceptance_criteria="A",
            key=None, source="paste", url=None,
        ),
        story_type=StoryType(
            choice="bug", confidence=0.7, probabilities={"bug": 0.7},
        ),
        blockers_failed=["has_persona"],
        to_discuss=[],
        model=None,
        request_id=None,
        latency_ms=None,
        input_tokens=None,
    )
    assert r.jev_model is None


def test_report_out_round_trip():
    r = _minimal_report()
    data = r.model_dump(by_alias=True)
    r2 = ReportOut(**data)
    assert r2.jev_model == r.jev_model
    assert r2.verdict == r.verdict


def test_report_out_none_optional_fields():
    r = _minimal_report(jev_model=None, request_id=None, latency_ms=None, input_tokens=None)
    data = r.model_dump(by_alias=True)
    assert data["model"] is None
    assert data["request_id"] is None


# ---------------------------------------------------------------------------
# SourceOut
# ---------------------------------------------------------------------------

def test_source_out_jira():
    s = SourceOut(name="jira", configured=True)
    assert s.name == "jira"
    assert s.configured is True


def test_source_out_linear():
    s = SourceOut(name="linear", configured=False)
    assert s.configured is False


def test_source_out_invalid_name():
    with pytest.raises(Exception):
        SourceOut(name="github", configured=True)


def test_source_out_round_trip():
    s = SourceOut(name="linear", configured=True)
    data = s.model_dump()
    assert SourceOut(**data) == s


# ---------------------------------------------------------------------------
# ErrorOut
# ---------------------------------------------------------------------------

def test_error_out_instantiation():
    e = ErrorOut(detail="Something went wrong")
    assert e.detail == "Something went wrong"


def test_error_out_round_trip():
    e = ErrorOut(detail="Bad input")
    data = e.model_dump()
    assert ErrorOut(**data) == e
