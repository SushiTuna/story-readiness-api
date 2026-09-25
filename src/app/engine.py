"""Assessment engine: pre-processing, check catalogue, and verdict logic."""

from __future__ import annotations

import re
from dataclasses import dataclass

import typesafe_sdk as ts

from app.jev_client import JevResponse, call_jev
from app.schemas import (
    AssessRequest,
    CheckKind,
    CheckOut,
    ReportOut,
    StoryOut,
    StorySource,
    StoryType,
    StoryTypeChoice,
    VerdictEnum,
)
from app.weights import DOR_WEIGHT, WEIGHTS

# ---------------------------------------------------------------------------
# Pre-processing helpers
# ---------------------------------------------------------------------------

_AC_HEADING = re.compile(
    r"(?:^|\n)"
    r"(?:#{1,3}\s*|(?:\*{2}))?"
    r"[Aa]cceptance\s+[Cc]riteria"
    r"(?:\*{2})?"
    r"[\s:]*\n",
)


def extract_ac(description: str, acceptance_criteria: str) -> tuple[str, str]:
    """Split an embedded AC section out of *description* when *acceptance_criteria* is empty.

    Searches for an "Acceptance Criteria" heading (Markdown ``##``, bold ``**...**``,
    or plain text at the start of a line) and splits the text at that point.

    Returns ``(cleaned_description, extracted_ac)``.
    """
    if acceptance_criteria.strip():
        return description, acceptance_criteria

    match = _AC_HEADING.search(description)
    if match is None:
        return description, acceptance_criteria

    split_pos = match.start() if description[match.start()] == "\n" else match.start()
    cleaned = description[:split_pos].rstrip()
    extracted = description[match.end():].strip()
    return cleaned, extracted


def normalise_dor(items: list[str]) -> list[str]:
    """Strip blanks, deduplicate (preserving order), truncate each to 300 chars, cap at 12."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in items:
        item = raw.strip()[:300]
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
        if len(result) == 12:
            break
    return result


# ---------------------------------------------------------------------------
# Check catalogue
# ---------------------------------------------------------------------------

@dataclass
class CheckDef:
    id: str
    label: str
    kind: CheckKind
    story_types: set[str] | None  # None = all types


CHECKS: list[CheckDef] = [
    CheckDef("ac_present",       "Acceptance criteria present",  CheckKind.blocker,  None),
    CheckDef("ac_quality",       "Acceptance criteria quality",  CheckKind.weighted, None),
    CheckDef("has_persona",      "User persona present",         CheckKind.blocker,  {"user_feature"}),
    CheckDef("value_statement",  "Statement of value",           CheckKind.weighted, {"user_feature", "bug"}),
    CheckDef("failure_handling", "Failure & edge cases",         CheckKind.weighted, {"user_feature", "bug"}),
    CheckDef("safe_rollout",     "Safe rollout described",       CheckKind.weighted, {"technical"}),
    CheckDef("title_clarity",    "Title clarity",                CheckKind.weighted, None),
    CheckDef("scope_size",       "Scope / story size",           CheckKind.flag,     None),
]

# Criteria lists for Score questions (used both when building questions and when mapping answers).
_AC_QUALITY_CRITERIA    = ["No ACs", "Some ACs, not testable", "Clear and testable ACs"]
_TITLE_CLARITY_CRITERIA = ["Vague", "Somewhat clear", "Clear and specific"]


# ---------------------------------------------------------------------------
# Main assess function
# ---------------------------------------------------------------------------

async def assess(
    request: AssessRequest,
    source: str = "paste",
    key: str | None = None,
    url: str | None = None,
) -> ReportOut:
    """Run the full assessment pipeline and return a :class:`ReportOut`."""

    # 1. Pre-process
    description, ac = extract_ac(request.description, request.acceptance_criteria)
    dor_items = normalise_dor(request.definition_of_ready)

    # 2. Build state
    state: dict = {
        "title": request.title,
        "description": description,
        "acceptance_criteria": ac,
    }

    # 3. Build questions
    questions: dict[str, ts.Noul | ts.Choice | ts.Score] = {
        "story_type": ts.Choice(
            criteria={
                "user_feature": "Feature for a user",
                "technical":    "Technical/infrastructure task",
                "bug":          "Bug fix",
            }
        ),
        "ac_quality": ts.Score(
            criteria=_AC_QUALITY_CRITERIA,
            instructions="Rate the acceptance criteria quality",
        ),
        "has_persona": ts.Noul(instructions="Does the story identify a user persona or role?"),
        "value_statement": ts.Noul(instructions="Does the story state the value or impact?"),
        "failure_handling": ts.Noul(instructions="Are failure cases and edge cases addressed?"),
        "safe_rollout": ts.Noul(instructions="Is rollout safety or migration described?"),
        "title_clarity": ts.Score(
            criteria=_TITLE_CLARITY_CRITERIA,
            instructions="Rate the clarity of the story title",
        ),
        "scope_size": ts.Noul(instructions="Does this story appear oversized or should be split?"),
    }
    for i, item in enumerate(dor_items):
        questions[f"dor_{i}"] = ts.Noul(instructions=f"Is the following requirement met: {item}")

    # 4. Call Jev
    jev: JevResponse = await call_jev(state, questions)

    # 5. Extract story type
    story_type_ans: ts.ChoiceAnswer = jev.answers["story_type"]
    story_type_str: str = story_type_ans.choice

    # 6. Filter checks to those applicable to the classified type
    applicable_checks = [
        c for c in CHECKS
        if c.story_types is None or story_type_str in c.story_types
    ]

    # 7. Evaluate ac_present in code
    ac_passed = bool(ac.strip())

    # 8. Map Jev answers → CheckOut
    check_outs: list[CheckOut] = []

    for chk in applicable_checks:
        if chk.id == "ac_present":
            value = 1.0 if ac_passed else 0.0
            check_outs.append(CheckOut(
                id=chk.id,
                label=chk.label,
                kind=chk.kind,
                value=value,
                passed=ac_passed,
                unsure=False,
                answer={"present": int(ac_passed)},
                ask=[],
            ))
            continue

        ans = jev.answers.get(chk.id)
        if ans is None:
            continue

        if isinstance(ans, ts.NoulAnswer):
            noul_val = ans.noul
            value = float(noul_val)
            passed = noul_val >= 0.5
            unsure = 0.35 <= noul_val <= 0.65
            answer: dict = {"yes": noul_val}
        elif isinstance(ans, ts.ScoreAnswer):
            criteria = _AC_QUALITY_CRITERIA if chk.id == "ac_quality" else _TITLE_CLARITY_CRITERIA
            max_score = len(criteria) - 1
            value = ans.score / max_score if max_score > 0 else 0.0
            passed = value >= 0.5
            unsure = ans.confidence < 0.6
            answer = {"score": ans.score, "max": max_score, "confidence": ans.confidence}
        else:
            continue

        check_outs.append(CheckOut(
            id=chk.id,
            label=chk.label,
            kind=chk.kind,
            value=value,
            passed=passed,
            unsure=unsure,
            answer=answer,
            ask=[],
        ))

    # DoR checks
    for i, item in enumerate(dor_items):
        dor_key = f"dor_{i}"
        ans = jev.answers.get(dor_key)
        if ans is None or not isinstance(ans, ts.NoulAnswer):
            continue
        noul_val = ans.noul
        value = float(noul_val)
        passed = noul_val >= 0.5
        unsure = 0.35 <= noul_val <= 0.65
        check_outs.append(CheckOut(
            id=dor_key,
            label=item[:80],
            kind=CheckKind.weighted,
            value=value,
            passed=passed,
            unsure=unsure,
            answer={"yes": noul_val},
            ask=[],
        ))

    # 9. Compute quality (weighted average of all 'weighted' checks)
    weighted_checks = [c for c in check_outs if c.kind == CheckKind.weighted]
    if weighted_checks:
        total_weight = 0.0
        total_score = 0.0
        for c in weighted_checks:
            w = WEIGHTS.get(c.id, DOR_WEIGHT)
            total_weight += w
            total_score += c.value * w
        quality = total_score / total_weight if total_weight > 0 else 0.0
    else:
        quality = 0.0

    # 10. Determine verdict (first-match)
    blockers_failed = [
        c.id for c in check_outs
        if c.kind == CheckKind.blocker and not c.passed and not c.unsure
    ]
    to_discuss = [c.id for c in check_outs if c.unsure]

    if blockers_failed:
        verdict = VerdictEnum.not_ready
    elif quality < 0.6:
        verdict = VerdictEnum.needs_refinement
    elif to_discuss:
        verdict = VerdictEnum.discuss
    else:
        verdict = VerdictEnum.ready

    # 11. Build and return ReportOut
    try:
        story_source = StorySource(source)
    except ValueError:
        story_source = StorySource.paste

    probs: dict[str, float] = {}
    if hasattr(story_type_ans, "probabilities") and story_type_ans.probabilities:
        probs = {k: float(v) for k, v in story_type_ans.probabilities.items()}

    return ReportOut(
        verdict=verdict,
        quality=round(quality, 4),
        checks=check_outs,
        story=StoryOut(
            title=request.title,
            description=description,
            acceptance_criteria=ac,
            key=key,
            source=story_source,
            url=url,
        ),
        story_type=StoryType(
            choice=StoryTypeChoice(story_type_str),
            confidence=float(story_type_ans.confidence),
            probabilities=probs,
        ),
        blockers_failed=blockers_failed,
        to_discuss=to_discuss,
        jev_model=jev.model,
        request_id=jev.request_id,
        latency_ms=jev.latency_ms,
        input_tokens=jev.input_tokens,
    )
