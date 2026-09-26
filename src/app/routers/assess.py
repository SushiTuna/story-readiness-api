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
from app.schemas import AssessRequest, ErrorOut, ImportRequest, ReportOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["assess"])


def _jira_configured() -> bool:
    return bool(os.getenv("JIRA_TOKEN") and os.getenv("JIRA_BASE_URL"))


def _linear_configured() -> bool:
    return bool(os.getenv("LINEAR_TOKEN"))


def _too_large(description: str, ac: str) -> JSONResponse | None:
    """Return a 413 JSONResponse if either field exceeds 10 000 chars, else None."""
    if len(description) > 10000 or len(ac) > 10000:
        return JSONResponse(
            status_code=413,
            content=ErrorOut(detail="The story is longer than the size limit.").model_dump(),
        )
    return None


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
    oversized = _too_large(body.description, body.acceptance_criteria)
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
        409: {"model": ErrorOut, "description": "The source's credentials are missing or were rejected."},
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
            return JSONResponse(
                status_code=409,
                content={"detail": "The source's credentials are not configured on the server."},
            )
        return JSONResponse(
            status_code=501,
            content={"detail": "Fetching from this source is not implemented yet."},
        )

    # ---------------------------------------------------------------- Linear
    if not _linear_configured():
        return JSONResponse(
            status_code=409,
            content={"detail": "The source's credentials are not configured on the server."},
        )

    # 1. Fetch issue from Linear
    try:
        issue = await fetch_issue(body.key)
    except LinearError as exc:
        logger.error("Linear fetch error (%s): %s", exc.kind, exc.detail)
        if exc.kind == "not_found":
            return JSONResponse(
                status_code=404,
                content=ErrorOut(detail="The issue was not found in the source.").model_dump(),
            )
        if exc.kind == "auth":
            return JSONResponse(
                status_code=409,
                content=ErrorOut(detail="The source's credentials were rejected. Check LINEAR_TOKEN on the server.").model_dump(),
            )
        return JSONResponse(
            status_code=502,
            content=ErrorOut(detail="The issue tracker is unavailable. Please try again later.").model_dump(),
        )

    # 2. Size check — runs before Jev so an oversized issue costs nothing
    oversized = _too_large(issue.description, "")
    if oversized is not None:
        return oversized

    # 3. Run the engine on the issue
    try:
        report = await assess_linear_issue(issue, body.definition_of_ready)
    except JevError as exc:
        logger.error("Upstream TypeSafe error: %s", exc.detail)
        return JSONResponse(
            status_code=502,
            content=ErrorOut(detail="The assessment service is unavailable. Please try again later.").model_dump(),
        )

    # 4. Optionally upsert a comment back to the issue (edit existing, create if absent)
    if body.post_comment:
        try:
            await write_report_comment(issue, report)
            response.headers["X-Linear-Comment"] = "posted"
        except LinearError as exc:
            logger.error("Linear comment error: %s", exc.detail)
            response.headers["X-Linear-Comment"] = "failed"

    return report
