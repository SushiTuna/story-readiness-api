"""POST /assess and POST /sources/{name}/assess routes."""
from __future__ import annotations

import logging
import os
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app import engine
from app.jev_client import JevError
from app.schemas import AssessRequest, ErrorOut, ImportRequest, ReportOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["assess"])


def _jira_configured() -> bool:
    return bool(os.getenv("JIRA_TOKEN") and os.getenv("JIRA_BASE_URL"))


def _linear_configured() -> bool:
    return bool(os.getenv("LINEAR_TOKEN"))


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
    if len(body.description) > 60000 or len(body.acceptance_criteria) > 60000:
        return JSONResponse(
            status_code=413,
            content=ErrorOut(detail="The story is longer than the size limit.").model_dump(),
        )
    try:
        return await engine.assess(body, source="paste")
    except JevError as exc:
        logger.error("Upstream TypeSafe error: %s", exc.detail)
        raise HTTPException(status_code=502, detail="The assessment service is unavailable. Please try again later.") from exc


@router.post(
    "/sources/{name}/assess",
    operation_id="assessFromSource",
    summary="Import a story from a tracker and assess it",
    tags=["assess", "sources"],
    responses={
        409: {"model": ErrorOut, "description": "Source credentials are not configured."},
        501: {"model": ErrorOut, "description": "Fetching from this source is not implemented yet."},
    },
)
async def assess_from_source(
    name: Literal["jira", "linear"],
    body: ImportRequest,
) -> JSONResponse:
    # 409 if credentials are not configured
    if name == "jira" and not _jira_configured():
        return JSONResponse(
            status_code=409,
            content={"detail": "The source's credentials are not configured on the server."},
        )
    if name == "linear" and not _linear_configured():
        return JSONResponse(
            status_code=409,
            content={"detail": "The source's credentials are not configured on the server."},
        )
    return JSONResponse(
        status_code=501,
        content={"detail": "Fetching from this source is not implemented yet."},
    )
