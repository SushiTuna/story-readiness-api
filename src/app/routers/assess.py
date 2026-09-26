"""POST /assess and POST /sources/{name}/assess routes."""
from __future__ import annotations

import logging
import os
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse

from app import engine
from app.jev_client import JevError
from app.linear_assess import assess_linear_issue
from app.linear_client import LinearError, fetch_issue, write_report_comment
from app.routers.errors import (
    jev_unavailable,
    linear_configured,
    linear_error_response,
    linear_not_configured,
    source_not_configured,
    too_large,
)
from app.schemas import AssessRequest, ErrorOut, ImportRequest, ReportOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["assess"])


def _jira_configured() -> bool:
    return bool(os.getenv("JIRA_TOKEN") and os.getenv("JIRA_BASE_URL"))


@router.post(
    "/assess",
    response_model=ReportOut,
    response_model_exclude_unset=False,
    operation_id="assessStory",
    summary="Assess a pasted story",
    tags=["assess"],
    responses={
        413: {"model": ErrorOut, "description": "The story is longer than the size limit."},
        502: {"model": ErrorOut, "description": "The TypeSafe request failed."},
    },
)
async def assess_story(body: AssessRequest) -> ReportOut:
    oversized = too_large(body.description, body.acceptance_criteria)
    if oversized is not None:
        return oversized
    try:
        return await engine.assess(body, source="paste")
    except JevError as exc:
        logger.error("Upstream TypeSafe error: %s", exc.detail)
        raise HTTPException(status_code=502, detail="The assessment service is unavailable. Please try again later.") from exc


@router.post(
    "/sources/{name}/assess",
    response_model=ReportOut,
    operation_id="assessFromSource",
    summary="Import a story from a tracker and assess it",
    description="Linear is implemented. Jira is still a stub and returns 501.",
    tags=["assess", "sources"],
    responses={
        200: {
            "headers": {
                "X-Linear-Comment": {
                    "description": (
                        "Present only when `post_comment` is true. `posted` if the comment was "
                        "written successfully, `failed` if the comment request failed (the "
                        "assessment is still returned)."
                    ),
                    "schema": {"type": "string", "enum": ["posted", "failed"]},
                },
            },
        },
        404: {"model": ErrorOut, "description": "The issue was not found in the source."},
        409: {"model": ErrorOut, "description": "The source is disabled, or its credentials are missing or were rejected."},
        413: {"model": ErrorOut, "description": "The story is longer than the size limit."},
        501: {"model": ErrorOut, "description": "Fetching from this source is not implemented yet (Jira)."},
        502: {"model": ErrorOut, "description": "The upstream service is unavailable."},
    },
)
async def assess_from_source(
    name: Literal["jira", "linear"],
    body: ImportRequest,
    response: Response,
) -> ReportOut | JSONResponse:
    # ------------------------------------------------------------------ Jira
    if name == "jira":
        if not _jira_configured():
            return source_not_configured()
        return JSONResponse(
            status_code=501,
            content={"detail": "Fetching from this source is not implemented yet."},
        )

    # ---------------------------------------------------------------- Linear
    if not linear_configured():
        return linear_not_configured()

    # 1. Fetch issue from Linear
    try:
        issue = await fetch_issue(body.key)
    except LinearError as exc:
        return linear_error_response(exc)

    # 2. Size check — runs before Jev so an oversized issue costs nothing
    oversized = too_large(issue.description, "")
    if oversized is not None:
        return oversized

    # 3. Run the engine on the issue
    try:
        report = await assess_linear_issue(issue, body.definition_of_ready)
    except JevError as exc:
        logger.error("Upstream TypeSafe error: %s", exc.detail)
        return jev_unavailable()

    # 4. Optionally upsert a comment back to the issue (edit existing, create if absent)
    if body.post_comment:
        try:
            await write_report_comment(issue, report)
            response.headers["X-Linear-Comment"] = "posted"
        except LinearError as exc:
            logger.error("Linear comment error: %s", exc.detail)
            response.headers["X-Linear-Comment"] = "failed"

    return report
