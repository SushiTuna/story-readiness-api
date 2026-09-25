"""Pydantic v2 models mirroring openapi.yaml components/schemas."""
from __future__ import annotations

import enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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


class SourceOut(BaseModel):
    name: Literal["jira", "linear"]
    configured: bool


class ErrorOut(BaseModel):
    detail: str
