"""Pydantic v2 models mirroring openapi.yaml components/schemas."""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class StoryTypeChoice(str, enum.Enum):
    user_feature = "user_feature"
    technical = "technical"
    bug = "bug"


class StorySource(str, enum.Enum):
    paste = "paste"
    jira = "jira"
    linear = "linear"


class CheckKind(str, enum.Enum):
    code = "code"
    blocker = "blocker"
    weighted = "weighted"
    flag = "flag"


class StoryStatus(str, enum.Enum):
    """Workflow column of a stored story on the board."""
    backlog = "backlog"
    refinement = "refinement"
    ready_for_sprint = "ready_for_sprint"
    in_sprint = "in_sprint"
    done = "done"
    blocked = "blocked"


class VerdictEnum(str, enum.Enum):
    ready = "ready"
    discuss = "discuss"
    needs_refinement = "needs_refinement"
    not_ready = "not_ready"


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class AssessRequest(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    title: str = Field(min_length=1, max_length=500)
    # max_length is intentionally absent from these two fields so that oversized
    # payloads reach the route handler and get a 413 (as the spec requires)
    # rather than a Pydantic 422.  The OpenAPI schema still advertises maxLength
    # via json_schema_extra so clients see the documented limit.
    description: str = Field(
        default="",
        json_schema_extra={"maxLength": 10000},
    )
    acceptance_criteria: str = Field(
        default="",
        json_schema_extra={"maxLength": 10000},
    )
    definition_of_ready: list[str] = Field(default_factory=list, max_length=50)


class ImportRequest(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    key: str = Field(min_length=1, max_length=100)
    definition_of_ready: list[str] = Field(default_factory=list, max_length=50)
    post_comment: bool = False


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------

class StoryOut(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    title: str
    description: str
    acceptance_criteria: str
    key: str | None
    source: StorySource
    url: str | None


class StoryType(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    choice: StoryTypeChoice
    confidence: float = Field(ge=0.0, le=1.0)
    probabilities: dict[str, float]


class CheckOut(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: str
    label: str
    kind: CheckKind
    value: float = Field(ge=0.0, le=1.0)
    passed: bool
    unsure: bool
    answer: dict[str, int | float | str]
    ask: list[str]


class ReportOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True, use_enum_values=True)

    verdict: VerdictEnum
    quality: float = Field(ge=0.0, le=1.0)
    checks: list[CheckOut]
    story: StoryOut
    story_type: StoryType
    blockers_failed: list[str]
    to_discuss: list[str]
    jev_model: str | None = Field(alias="model", serialization_alias="model")
    request_id: str | None
    latency_ms: int | None
    input_tokens: int | None
    agent_checks_skipped: bool = Field(
        False,
        description="The story is human-only, so the AI-agent readiness checks were not run and are not in `checks`, "
        "the quality score or the verdict.",
    )


class SourceOut(BaseModel):
    name: Literal["jira", "linear"]
    configured: bool


class ErrorOut(BaseModel):
    detail: str


# ---------------------------------------------------------------------------
# Boards
# ---------------------------------------------------------------------------

class BoardUpdateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)


class BoardIn(BoardUpdateIn):
    key_prefix: str = Field(
        min_length=2,
        max_length=6,
        pattern=r"^[A-Z][A-Z0-9]+$",
        description="Prefix of the board's story keys, e.g. FLW for FLW-1. Uppercase letters and digits, starting "
        "with a letter. Unique among boards and fixed once the board is created.",
    )


class BoardOut(BaseModel):
    id: str
    name: str
    description: str
    key_prefix: str
    created_at: datetime
    story_count: int = Field(description="Stories on the board, in every column.")
    story_limit: int = Field(description="The most stories the board can hold.")


# ---------------------------------------------------------------------------
# Stored stories
# ---------------------------------------------------------------------------

class StoryIn(AssessRequest):
    """A story to store. Same fields and limits as AssessRequest."""


class StoryCreateIn(StoryIn):
    """A new story to store, optionally split from another story on the same board."""

    parent_id: str | None = Field(
        None,
        description="ID of the story on the same board that this one is split from. A child of a human-only story "
        "is human-only too.",
    )
    human_only: bool = Field(
        False,
        description="Only people on the board may change the story; AI agents can't. It skips the AI-agent readiness "
        "checks.",
    )


class HumanOnlyIn(BaseModel):
    human_only: bool = Field(
        description="True: only people on the board may change the story; AI agents can't, and assessments skip the "
        "AI-agent readiness checks. Stories split from it keep their tag when it is cleared."
    )


class EvidenceItemIn(BaseModel):
    """One piece of evidence: a file uploaded with POST /stories/{story_id}/evidence, or a link."""

    model_config = ConfigDict(str_strip_whitespace=True)

    file_id: str | None = Field(None, description="ID of an evidence file uploaded for this story and not used yet.")
    url: str | None = Field(
        None, max_length=2000, pattern=r"^https?://\S+$", description="Link to evidence kept elsewhere, e.g. a CI run."
    )
    caption: str | None = Field(None, max_length=200, description="Optional short summary, e.g. '42 passed'.")

    @model_validator(mode="after")
    def _file_or_url(self) -> EvidenceItemIn:
        if (self.file_id is None) == (self.url is None):
            raise PydanticCustomError("evidence_source", "Give exactly one of file_id or url.")
        return self


class TestReportIn(EvidenceItemIn):
    __test__ = False  # not a pytest test class

    kind: Literal["unit", "integration", "cucumber"]


class CommitIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    hash: str = Field(pattern=r"^[0-9a-fA-F]{7,40}$", description="Commit hash, 7–40 hex characters.")
    message: str = Field(min_length=1, max_length=500, description="The commit's description.")

    @model_validator(mode="after")
    def _lowercase_hash(self) -> CommitIn:
        self.hash = self.hash.lower()
        return self


class DoneEvidenceIn(BaseModel):
    """What moving a story into Done requires: test reports, UI evidence for UI changes, and the commits."""

    test_reports: list[TestReportIn] = Field(min_length=1, max_length=10)
    ui_change: bool = Field(description="The story changes the UI; then ui_evidence needs a screenshot or recording.")
    ui_evidence: list[EvidenceItemIn] = Field(default_factory=list, max_length=10)
    commits: list[CommitIn] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _ui_evidence_for_ui_changes(self) -> DoneEvidenceIn:
        if self.ui_change and not self.ui_evidence:
            raise PydanticCustomError("ui_evidence_missing", "A UI change needs at least one screenshot or recording.")
        return self


class StoryMoveIn(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    status: StoryStatus
    position: float = Field(description="Order within the column, ascending. The client picks a value between the neighbours.")
    blocked_reason: str | None = Field(
        None,
        max_length=500,
        description="Why the story is blocked. Required when status is blocked; not allowed otherwise.",
    )
    done_evidence: DoneEvidenceIn | None = Field(
        None,
        description="Evidence that the story is done. Required when the story enters the done column; not allowed "
        "for other columns.",
    )

    @model_validator(mode="after")
    def _reason_only_when_blocked(self) -> StoryMoveIn:
        # PydanticCustomError (not ValueError) keeps the 422 body JSON-serialisable in main's handler.
        reason = self.blocked_reason.strip() if self.blocked_reason is not None else None
        if self.status == StoryStatus.blocked.value and not reason:
            raise PydanticCustomError("blocked_reason_missing", "A blocked story needs a blocked_reason.")
        if self.status != StoryStatus.blocked.value and reason is not None:
            raise PydanticCustomError("blocked_reason_not_allowed", "blocked_reason is only allowed when status is blocked.")
        self.blocked_reason = reason
        # Whether evidence is required depends on the story's current column, which only the store knows.
        if self.status != StoryStatus.done.value and self.done_evidence is not None:
            raise PydanticCustomError("done_evidence_not_allowed", "done_evidence is only allowed when status is done.")
        return self


class EvidenceFileOut(BaseModel):
    id: str
    filename: str
    content_type: str
    size: int = Field(description="Size in bytes.")
    created_at: datetime


class AssessmentSummaryOut(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    verdict: VerdictEnum
    quality: float = Field(ge=0.0, le=1.0)
    question_count: int = Field(description="Questions for the author in this assessment.")
    created_at: datetime


class ActivityOut(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    actor_kind: Literal["agent", "user"] = Field(description="`agent` for the MCP server, `user` for the HTTP API.")
    actor: str = Field(description="Agent name, or 'board' for the web UI.")
    action: Literal[
        "board_created",
        "board_updated",
        "story_created",
        "story_edited",
        "story_moved",
        "story_assessed",
        "story_deleted",
        "story_human_only",
    ]
    detail: dict = Field(
        description="Action-specific data, e.g. {\"fields\": [\"title\"]} or {\"from\": \"backlog\", \"to\": \"blocked\"}. "
        "A move into done has an \"evidence\" object: test_reports, ui_change, ui_evidence and commits, with each "
        "uploaded file's id, filename, content_type and size under \"file\". A split story's story_created has "
        "\"parent_key\". story_human_only has {\"human_only\": true|false}, and \"inherited_from\" (the parent's key) "
        "when a split story inherited the tag."
    )
    note: str | None = Field(description="The agent's reason for the change, or null for UI changes.")
    story_key: str | None = Field(description="Human-readable story key, kept after the story is deleted.")
    created_at: datetime


class StoredStoryOut(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: str
    board_id: str
    key: str = Field(
        description="Short, human-readable key: the board's prefix and a number, e.g. FLW-12. Never reused after a delete."
    )
    title: str
    description: str
    acceptance_criteria: str
    definition_of_ready: list[str]
    created_at: datetime
    updated_at: datetime
    status: StoryStatus
    position: float
    blocked_reason: str | None = Field(description="Why the story is blocked; set only in the blocked column.")
    latest: AssessmentSummaryOut | None = Field(description="The latest assessment, or null if never assessed.")
    previous_quality: float | None = Field(description="Quality of the assessment before the latest one.")
    stale: bool = Field(description="The story was edited after its latest assessment.")
    agents: list[str] = Field(default_factory=list, description="Names of agents that have acted on this story, oldest first.")
    human_only: bool = Field(
        description="Only people on the board may change the story; AI agents can't. Set with PUT …/human-only."
    )
    parent_id: str | None = Field(description="ID of the story it was split from, or null.")
    parent_key: str | None = Field(description="Key of the story it was split from, e.g. FLW-3, or null.")


class StoredStoryDetailOut(StoredStoryOut):
    report: ReportOut | None = Field(description="Full report of the latest assessment.")
    history: list[AssessmentSummaryOut] = Field(description="All assessments, newest first.")
    activity: list[ActivityOut] = Field(default_factory=list, description="Activity log for this story, newest first.")


# ---------------------------------------------------------------------------
# Linear listing
# ---------------------------------------------------------------------------

class LinearTeamOut(BaseModel):
    id: str
    key: str
    name: str


class IssueCardOut(BaseModel):
    key: str
    title: str
    description: str
    url: str
    labels: list[str]
    state_type: str = Field(description="Linear workflow state type, e.g. `backlog`, `unstarted` or `started`.")
    readiness: str | None = Field(
        description="Suffix of the issue's readiness:* label, e.g. `ready`, `stale` or `error`; null if none.",
    )
