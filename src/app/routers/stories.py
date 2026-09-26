"""Stored (pasted) stories and their assessment history, kept in SQLite.

CRUD handlers are plain ``def`` so FastAPI runs the blocking sqlite3 calls in
its threadpool; the assess handler is async and hops to the threadpool
explicitly around the store calls.
"""
from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app import engine, story_store
from app.jev_client import JevError
from app.routers.errors import board_full, board_not_found, jev_unavailable, too_large
from app.schemas import (
    AssessmentSummaryOut,
    AssessRequest,
    ErrorOut,
    ReportOut,
    StoredStoryDetailOut,
    StoredStoryOut,
    StoryIn,
    StoryMoveIn,
)
from app.story_store import Assessment, StoredStory

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stories"])

_NOT_FOUND = {404: {"model": ErrorOut, "description": "The story was not found."}}
_BOARD_NOT_FOUND = {404: {"model": ErrorOut, "description": "The board was not found."}}
_BOARD_FULL = {409: {"model": ErrorOut, "description": "The board already holds the most stories it can."}}
_TOO_LARGE = {413: {"model": ErrorOut, "description": "The story is longer than the size limit."}}


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content=ErrorOut(detail="The story was not found.").model_dump())


def _summary(assessment: Assessment) -> AssessmentSummaryOut:
    return AssessmentSummaryOut(
        verdict=assessment.verdict,
        quality=assessment.quality,
        question_count=assessment.question_count,
        created_at=assessment.created_at,
    )


def _fields(story: StoredStory, recent: list[Assessment]) -> dict:
    """Output fields shared by the list and detail views; *recent* is newest first."""
    latest = recent[0] if recent else None
    return {
        "id": story.id,
        "board_id": story.board_id,
        "key": story.key,
        "title": story.title,
        "description": story.description,
        "acceptance_criteria": story.acceptance_criteria,
        "definition_of_ready": story.definition_of_ready,
        "created_at": story.created_at,
        "updated_at": story.updated_at,
        "status": story.status,
        "position": story.position,
        "blocked_reason": story.blocked_reason,
        "latest": _summary(latest) if latest else None,
        "previous_quality": recent[1].quality if len(recent) > 1 else None,
        "stale": latest is not None and latest.fingerprint != story.fingerprint,
    }


def _story_kwargs(body: StoryIn) -> dict:
    return {
        "title": body.title,
        "description": body.description,
        "acceptance_criteria": body.acceptance_criteria,
        "definition_of_ready": body.definition_of_ready,
    }


@router.get(
    "/boards/{board_id}/stories",
    response_model=list[StoredStoryOut],
    operation_id="listStories",
    summary="List a board's stored stories with their latest assessment",
    responses=_BOARD_NOT_FOUND,
)
def list_stories(board_id: str) -> list[StoredStoryOut] | JSONResponse:
    if story_store.get_board(board_id) is None:
        return board_not_found()
    return [StoredStoryOut(**_fields(story, recent)) for story, recent in story_store.list_stories(board_id)]


@router.post(
    "/boards/{board_id}/stories",
    response_model=StoredStoryOut,
    status_code=201,
    operation_id="createStory",
    summary="Store a pasted story on a board",
    description=f"Adds the story to the end of the board's backlog. A board holds at most "
    f"{story_store.MAX_STORIES_PER_BOARD} stories, counting every column.",
    responses={**_BOARD_NOT_FOUND, **_BOARD_FULL, **_TOO_LARGE},
)
def create_story(board_id: str, body: StoryIn) -> StoredStoryOut | JSONResponse:
    oversized = too_large(body.description, body.acceptance_criteria)
    if oversized is not None:
        return oversized
    try:
        story = story_store.create_story(board_id, **_story_kwargs(body))
    except story_store.BoardNotFoundError:
        return board_not_found()
    except story_store.BoardFullError:
        return board_full()
    return StoredStoryOut(**_fields(story, []))


@router.get(
    "/stories/{story_id}",
    response_model=StoredStoryDetailOut,
    operation_id="getStory",
    summary="Get a stored story with its latest report and assessment history",
    responses=_NOT_FOUND,
)
def get_story(story_id: str) -> StoredStoryDetailOut | JSONResponse:
    story = story_store.get_story(story_id)
    if story is None:
        return _not_found()
    history = story_store.list_assessments(story_id)
    return StoredStoryDetailOut(
        **_fields(story, history),
        report=history[0].report() if history else None,
        history=[_summary(a) for a in history],
    )


@router.put(
    "/stories/{story_id}",
    response_model=StoredStoryOut,
    operation_id="updateStory",
    summary="Replace a stored story's text",
    description="Earlier assessments are kept; the story is marked stale until it is assessed again.",
    responses={**_NOT_FOUND, **_TOO_LARGE},
)
def update_story(story_id: str, body: StoryIn) -> StoredStoryOut | JSONResponse:
    oversized = too_large(body.description, body.acceptance_criteria)
    if oversized is not None:
        return oversized
    story = story_store.update_story(story_id, **_story_kwargs(body))
    if story is None:
        return _not_found()
    recent = story_store.list_assessments(story_id)[:2]
    return StoredStoryOut(**_fields(story, recent))


@router.put(
    "/stories/{story_id}/move",
    response_model=StoredStoryOut,
    operation_id="moveStory",
    summary="Move a stored story to a workflow column and position",
    description=(
        "Used by drag and drop on the board. Does not change the story's text or mark it stale. "
        "Moving to the blocked column requires a blocked_reason; moving anywhere else clears it."
    ),
    responses=_NOT_FOUND,
)
def move_story(story_id: str, body: StoryMoveIn) -> StoredStoryOut | JSONResponse:
    story = story_store.move_story(
        story_id, status=body.status, position=body.position, blocked_reason=body.blocked_reason
    )
    if story is None:
        return _not_found()
    recent = story_store.list_assessments(story_id)[:2]
    return StoredStoryOut(**_fields(story, recent))


@router.delete(
    "/stories/{story_id}",
    status_code=204,
    response_class=Response,
    operation_id="deleteStory",
    summary="Delete a stored story and its assessment history",
    responses=_NOT_FOUND,
)
def delete_story(story_id: str) -> Response:
    if not story_store.delete_story(story_id):
        return _not_found()
    return Response(status_code=204)


@router.post(
    "/stories/{story_id}/assess",
    response_model=ReportOut,
    operation_id="assessStoredStory",
    summary="Assess a stored story and save the report to its history",
    responses={
        **_NOT_FOUND,
        **_TOO_LARGE,
        502: {"model": ErrorOut, "description": "The TypeSafe request failed."},
    },
)
async def assess_stored_story(story_id: str) -> ReportOut | JSONResponse:
    story = await run_in_threadpool(story_store.get_story, story_id)
    if story is None:
        return _not_found()
    oversized = too_large(story.description, story.acceptance_criteria)
    if oversized is not None:
        return oversized

    request = AssessRequest(
        title=story.title,
        description=story.description,
        acceptance_criteria=story.acceptance_criteria,
        definition_of_ready=story.definition_of_ready,
    )
    try:
        report = await engine.assess(request, source="paste")
    except JevError as exc:
        logger.error("Upstream TypeSafe error: %s", exc.detail)
        return jev_unavailable()

    try:
        await run_in_threadpool(story_store.add_assessment, story, report)
    except sqlite3.IntegrityError:
        # The story was deleted while Jev was answering.
        return _not_found()
    return report
