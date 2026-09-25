"""Assessment engine: pre-processing, check catalogue, and verdict logic."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import typesafe_sdk as ts

logger = logging.getLogger(__name__)

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
    CheckDef("value_statement",  "Statement of value",           CheckKind.weighted, {"user_feature"}),
    CheckDef("failure_handling", "Failure & edge cases",         CheckKind.weighted, {"user_feature", "bug"}),
    CheckDef("safe_rollout",     "Safe rollout described",       CheckKind.weighted, {"technical"}),
    CheckDef("title_clarity",    "Title clarity",                CheckKind.weighted, None),
    CheckDef("scope_size",       "Scope / story size",           CheckKind.flag,     None),
]

# Criteria lists for Score questions (used both when building questions and when mapping answers).
_AC_QUALITY_CRITERIA    = ["No ACs", "Some ACs, not testable", "Clear and testable ACs"]
_TITLE_CLARITY_CRITERIA = ["Vague", "Somewhat clear", "Clear and specific"]

# Questions to surface to the author when a check fails or the model is unsure.
_ASK: dict[str, list[str]] = {
    "ac_present":      ["Please add acceptance criteria so the team can verify when this story is done."],
    "ac_quality":      ["Can you rewrite the acceptance criteria in Given/When/Then or a similarly testable format?"],
    "has_persona":     ["Who is the primary user or role that benefits from this story?"],
    "value_statement": ["What business or user value does completing this story deliver?"],
    "failure_handling":["What should happen when this feature fails or encounters an edge case?"],
    "safe_rollout":    ["How will this change be rolled out safely — e.g. feature flag, migration plan, or rollback strategy?"],
    "title_clarity":   ["Can you make the title more specific so it clearly conveys the change being made?"],
    "scope_size":      ["This story may be too large to complete in a single sprint — can it be split?"],
}


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
            instructions="What kind of work does the story (`title`, `description`, `acceptance_criteria`) describe?",
            criteria={
                "user_feature": "New or changed behaviour that users, customers or API clients can see or use; the story delivers value directly to them.",
                "technical":    "Internal engineering work that changes how the system is built or run rather than what its users can do, e.g. infrastructure, maintenance or developer tooling.",
                "bug":          "Existing behaviour is broken or differs from what is expected; the story fixes a defect, crash or regression.",
            },
        ),
        "ac_quality": ts.Score(
            criteria=_AC_QUALITY_CRITERIA,
            instructions="Rate the acceptance criteria quality",
        ),
        "has_persona": ts.Noul(
            instructions="Do `title` or `description` name who the story is for?",
            criteria={
                "true":  "A specific user, role or client is named as the beneficiary, e.g. 'As a registered user', 'As an API client developer', 'admins'.",
                "false": "No beneficiary is named, or only a vague 'we', 'the system' or 'someone'.",
            },
        ),
        "value_statement": ts.Noul(
            instructions="Does `description` say why the change matters?",
            criteria={
                "true":  "It states the benefit or outcome, e.g. a 'so that …' clause, a business reason, or the problem it removes.",
                "false": "It only says what to build or what is broken, with no reason or benefit.",
            },
        ),
        "failure_handling": ts.Noul(
            instructions="Do `description` or `acceptance_criteria` cover what happens on failure, invalid input or boundary values?",
            criteria={
                "true":  "At least one error path, invalid input, limit or boundary case is specified with its expected result.",
                "false": "Only the happy path is described, or failure behaviour is left unspecified.",
            },
        ),
        "safe_rollout": ts.Noul(
            instructions="Do `description` or `acceptance_criteria` explain how the change is released safely?",
            criteria={
                "true":  "A rollout or rollback mechanism is described, e.g. feature flag, dual writes, phased migration, backfill check, or rollback step.",
                "false": "The change is described with no release, migration or rollback plan.",
            },
        ),
        "title_clarity": ts.Score(
            criteria=_TITLE_CLARITY_CRITERIA,
            instructions="Rate the clarity of the story title",
        ),
        "scope_size": ts.Noul(
            instructions="Is the story small enough for one team to finish within a single sprint?",
            criteria={
                "true":  "It describes one focused change or fix with a handful of acceptance criteria.",
                "false": "It bundles several features, systems or deliverables that should be split into separate stories.",
            },
        ),
    }
    for i, item in enumerate(dor_items):
        questions[f"dor_{i}"] = ts.Noul(
            instructions={
                "requirement": item,
                "question":    "Does the story (`title`, `description`, `acceptance_criteria`) satisfy `requirement`?",
            },
            criteria={
                "true":  "The story text explicitly addresses the requirement.",
                "false": "The story text does not mention or address the requirement.",
            },
        )

    # 4. Call Jev
    jev_model: str | None = os.getenv("TYPESAFE_MODEL") or None
    jev: JevResponse = await call_jev(state, questions, model=jev_model)

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
                ask=_ASK.get(chk.id, []) if not ac_passed else [],
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
            ask=_ASK.get(chk.id, []) if (not passed or unsure) else [],
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
            ask=[f"Please confirm: {item[:200]}"] if (not passed or unsure) else [],
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
    # flag checks are display-only — they must never influence the verdict.
    blockers_failed = [
        c.id for c in check_outs
        if c.kind == CheckKind.blocker and not c.passed and not c.unsure
    ]
    to_discuss = [
        c.id for c in check_outs
        if c.unsure and c.kind != CheckKind.flag
    ]

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
