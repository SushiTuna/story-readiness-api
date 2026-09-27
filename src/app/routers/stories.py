"""Stored (pasted) stories and their assessment history, kept in SQLite.

CRUD handlers are plain ``def`` so FastAPI runs the blocking sqlite3 calls in
its threadpool; the assess handler is async and hops to the threadpool
explicitly around the store calls.
"""
from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, File, Query, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from app import engine, story_store
from app.jev_client import JevError
from app.routers.errors import (
    blocker_invalid,
    blockers_open,
    board_full,
    board_not_found,
    evidence_attached,
    evidence_invalid,
    evidence_not_found,
    evidence_too_large,
    evidence_type_not_allowed,
    jev_unavailable,
    parent_invalid,
    status_not_allowed,
    too_large,
)
from app.schemas import (
    ActivityOut,
    AssessmentSummaryOut,
    AssessRequest,
    BlockerIn,
    ErrorOut,
    EvidenceFileOut,
    HumanOnlyIn,
    ReportOut,
    StoredStoryDetailOut,
    StoredStoryOut,
    StoryCreateIn,
    StoryIn,
    StoryMoveIn,
    StoryRefOut,
)
from app.story_store import Assessment, StoredStory, StoryRef

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stories"])

_NOT_FOUND = {404: {"model": ErrorOut, "description": "The story was not found."}}
_BOARD_NOT_FOUND = {404: {"model": ErrorOut, "description": "The board was not found."}}
_BOARD_FULL = {409: {"model": ErrorOut, "description": "The board already holds the most stories it can."}}
_TOO_LARGE = {413: {"model": ErrorOut, "description": "The story is longer than the size limit."}}
_EVIDENCE_NOT_FOUND = {404: {"model": ErrorOut, "description": "The evidence file was not found."}}


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content=ErrorOut(detail="The story was not found.").model_dump())


def _summary(assessment: Assessment) -> AssessmentSummaryOut:
    return AssessmentSummaryOut(
        verdict=assessment.verdict,
        quality=assessment.quality,
        question_count=assessment.question_count,
        created_at=assessment.created_at,
    )


def _activity_out(entry: story_store.ActivityEntry) -> ActivityOut:
    return ActivityOut(
        actor_kind=entry.actor_kind,
        actor=entry.actor,
        action=entry.action,
        detail=entry.detail,
        note=entry.note,
        story_key=entry.story_key,
        created_at=entry.created_at,
    )


def _ref(ref: StoryRef) -> StoryRefOut:
    return StoryRefOut(id=ref.id, key=ref.key, title=ref.title, status=ref.status)


def _fields(story: StoredStory, recent: list[Assessment], agents: list[str] | None = None) -> dict:
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
        "agents": agents if agents is not None else [],
        "human_only": story.human_only,
        "parent_id": story.parent_id,
        "parent_key": story.parent_key,
        "tags": story.tags,
        "blocked_by": [_ref(r) for r in story.blocked_by],
        "blocks": [_ref(r) for r in story.blocks],
    }


def _story_kwargs(body: StoryIn) -> dict:
    return {
        "title": body.title,
        "description": body.description,
        "acceptance_criteria": body.acceptance_criteria,
        "definition_of_ready": body.definition_of_ready,
        # Left out: the story keeps its tags (a new story has none).
        "tags": body.tags if "tags" in body.model_fields_set else None,
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
    pairs = story_store.list_stories(board_id)
    agents_map = story_store.agents_by_story(board_id)
    return [
        StoredStoryOut(**_fields(story, recent, agents_map.get(story.id, [])))
        for story, recent in pairs
    ]


@router.get(
    "/boards/{board_id}/activity",
    response_model=list[ActivityOut],
    operation_id="listBoardActivity",
    summary="List activity on a board, newest first",
    responses=_BOARD_NOT_FOUND,
)
def list_board_activity(
    board_id: str,
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of entries to return."),
) -> list[ActivityOut] | JSONResponse:
    if story_store.get_board(board_id) is None:
        return board_not_found()
    entries = story_store.list_activity(board_id, limit=limit)
    return [_activity_out(e) for e in entries]


@router.post(
    "/boards/{board_id}/stories",
    response_model=StoredStoryOut,
    status_code=201,
    operation_id="createStory",
    summary="Store a pasted story on a board",
    description=f"Adds the story to the end of the board's backlog. A board holds at most "
    f"{story_store.MAX_STORIES_PER_BOARD} stories, counting every column. With parent_id it is split from another "
    "story on the board; a child of a human-only story is human-only too.",
    responses={**_BOARD_NOT_FOUND, **_BOARD_FULL, **_TOO_LARGE},
)
def create_story(board_id: str, body: StoryCreateIn) -> StoredStoryOut | JSONResponse:
    oversized = too_large(body.description, body.acceptance_criteria)
    if oversized is not None:
        return oversized
    try:
        story = story_store.create_story(
            board_id, **_story_kwargs(body), parent_id=body.parent_id, human_only=body.human_only
        )
    except story_store.BoardNotFoundError:
        return board_not_found()
    except story_store.BoardFullError:
        return board_full()
    except story_store.ParentNotFoundError:
        return parent_invalid("The story to split from was not found on this board.")
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
    agents_map = story_store.agents_by_story(story.board_id)
    activity = story_store.list_activity(story.board_id, story_id=story_id)
    return StoredStoryDetailOut(
        **_fields(story, history, agents_map.get(story_id, [])),
        report=history[0].report() if history else None,
        history=[_summary(a) for a in history],
        activity=[_activity_out(e) for e in activity],
    )


@router.put(
    "/stories/{story_id}",
    response_model=StoredStoryOut,
    operation_id="updateStory",
    summary="Replace a stored story's text and tags",
    description="Earlier assessments are kept. Changing the text marks the story stale until it is assessed again; "
    "changing only the tags does not. Leave tags out to keep the current ones.",
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
        "Moving to the blocked column requires a blocked_reason; moving anywhere else clears it. "
        "Entering the done column requires done_evidence (test reports, UI evidence for UI changes, commits); "
        "it is recorded in the story's activity. "
        "While the latest assessment's verdict is discuss or needs_refinement (even if stale), the story can only "
        "move to backlog or refinement, or within its current column, until it is assessed again. "
        "A story can't enter in_sprint or done while a story it waits on (blocked_by) is not done."
    ),
    responses=_NOT_FOUND,
)
def move_story(story_id: str, body: StoryMoveIn) -> StoredStoryOut | JSONResponse:
    evidence = body.done_evidence.model_dump() if body.done_evidence is not None else None
    try:
        story = story_store.move_story(
            story_id, status=body.status, position=body.position, blocked_reason=body.blocked_reason,
            done_evidence=evidence,
        )
    except story_store.ReadinessError as exc:
        return status_not_allowed(str(exc))
    except story_store.BlockersOpenError as exc:
        return blockers_open(str(exc))
    except story_store.EvidenceError as exc:
        return evidence_invalid(str(exc))
    if story is None:
        return _not_found()
    recent = story_store.list_assessments(story_id)[:2]
    return StoredStoryOut(**_fields(story, recent))


@router.put(
    "/stories/{story_id}/human-only",
    response_model=StoredStoryOut,
    operation_id="setStoryHumanOnly",
    summary="Set or clear a stored story's human-only tag",
    description=(
        "A human-only story depends on a person (accounts, servers, another team's API, or secrets): AI agents using "
        "the MCP server can read it but not change it, and assessments skip the AI-agent readiness checks. Only this "
        "API sets or clears the tag. Stories already split from it keep theirs. The latest assessment turns stale."
    ),
    responses=_NOT_FOUND,
)
def set_human_only(story_id: str, body: HumanOnlyIn) -> StoredStoryOut | JSONResponse:
    story = story_store.set_human_only(story_id, body.human_only)
    if story is None:
        return _not_found()
    recent = story_store.list_assessments(story_id)[:2]
    agents_map = story_store.agents_by_story(story.board_id)
    return StoredStoryOut(**_fields(story, recent, agents_map.get(story_id, [])))


@router.post(
    "/stories/{story_id}/blockers",
    response_model=StoredStoryOut,
    operation_id="addStoryBlocker",
    summary="Make a stored story wait on another story on its board",
    description=(
        "The story can't enter in_sprint or done until the story it waits on is done; it can still move anywhere "
        "else. Both stories must be on the same board, and a story can't wait on itself, directly or through other "
        "stories. Adding a dependency that exists already changes nothing. Does not mark the story stale."
    ),
    responses=_NOT_FOUND,
)
def add_blocker(story_id: str, body: BlockerIn) -> StoredStoryOut | JSONResponse:
    try:
        story = story_store.add_blocker(story_id, body.blocker_id)
    except story_store.BlockerNotFoundError:
        return blocker_invalid("The story to wait on was not found on this board.")
    except story_store.DependencyCycleError as exc:
        return blocker_invalid(str(exc), "dependency_cycle")
    if story is None:
        return _not_found()
    return _out(story)


@router.delete(
    "/stories/{story_id}/blockers/{blocker_id}",
    response_model=StoredStoryOut,
    operation_id="removeStoryBlocker",
    summary="Stop a stored story waiting on another story",
    description="Removing a dependency that does not exist changes nothing.",
    responses=_NOT_FOUND,
)
def remove_blocker(story_id: str, blocker_id: str) -> StoredStoryOut | JSONResponse:
    story = story_store.remove_blocker(story_id, blocker_id)
    if story is None:
        return _not_found()
    return _out(story)


def _out(story: StoredStory) -> StoredStoryOut:
    recent = story_store.list_assessments(story.id)[:2]
    agents_map = story_store.agents_by_story(story.board_id)
    return StoredStoryOut(**_fields(story, recent, agents_map.get(story.id, [])))


@router.post(
    "/stories/{story_id}/evidence",
    response_model=EvidenceFileOut,
    status_code=201,
    operation_id="uploadEvidence",
    summary="Upload an evidence file for a move to Done",
    description=(
        f"A test report ({', '.join(e for e, t in story_store.EVIDENCE_TYPES.items() if not t.startswith(('image/', 'video/')))}) "
        f"or a screenshot or recording ({', '.join(e for e, t in story_store.EVIDENCE_TYPES.items() if t.startswith(('image/', 'video/')))}), "
        f"at most {story_store.MAX_EVIDENCE_BYTES // (1024 * 1024)} MB. Pass the returned id in done_evidence when "
        "moving the story to Done."
    ),
    responses={
        **_NOT_FOUND,
        413: {"model": ErrorOut, "description": "The evidence file is larger than the size limit."},
        415: {"model": ErrorOut, "description": "The evidence file's type is not allowed."},
    },
)
def upload_evidence(story_id: str, file: UploadFile = File(...)) -> EvidenceFileOut | JSONResponse:
    try:
        story_store.evidence_content_type(file.filename or "")
    except story_store.EvidenceFileTypeError:
        return evidence_type_not_allowed()
    data = file.file.read(story_store.MAX_EVIDENCE_BYTES + 1)  # one byte over tells us it is too large
    try:
        saved = story_store.save_evidence_file(story_id, file.filename or "", data)
    except story_store.EvidenceFileTooLargeError:
        return evidence_too_large()
    if saved is None:
        return _not_found()
    return EvidenceFileOut(
        id=saved.id, filename=saved.filename, content_type=saved.content_type, size=saved.size,
        created_at=saved.created_at,
    )


# Screenshots and recordings show inline in the activity log; anything else downloads, so an uploaded HTML
# report can never run scripts on the board's origin.
_INLINE_TYPES = ("image/", "video/")


@router.get(
    "/evidence/{file_id}",
    response_class=FileResponse,
    operation_id="getEvidence",
    summary="Download an evidence file",
    responses={200: {"description": "The file.", "content": {"application/octet-stream": {}}}, **_EVIDENCE_NOT_FOUND},
)
def get_evidence(file_id: str) -> Response:
    file = story_store.get_evidence_file(file_id)
    path = story_store.evidence_path(file) if file else None
    if file is None or not path.is_file():
        return evidence_not_found()
    inline = file.content_type.startswith(_INLINE_TYPES)
    return FileResponse(
        path,
        media_type=file.content_type,
        filename=file.filename,
        content_disposition_type="inline" if inline else "attachment",
        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"},
    )


@router.delete(
    "/evidence/{file_id}",
    status_code=204,
    response_class=Response,
    operation_id="deleteEvidence",
    summary="Delete an evidence file that no move has used yet",
    responses={
        **_EVIDENCE_NOT_FOUND,
        409: {"model": ErrorOut, "description": "The evidence file is part of a move to Done, so it is kept."},
    },
)
def delete_evidence(file_id: str) -> Response:
    try:
        deleted = story_store.delete_unattached_evidence_file(file_id)
    except story_store.EvidenceFileAttachedError:
        return evidence_attached()
    return Response(status_code=204) if deleted else evidence_not_found()


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
        report = await engine.assess(request, source="paste", skip_agent_checks=story.human_only)
    except JevError as exc:
        logger.error("Upstream TypeSafe error: %s", exc.detail)
        return jev_unavailable()

    try:
        await run_in_threadpool(story_store.add_assessment, story, report)
    except sqlite3.IntegrityError:
        # The story was deleted while Jev was answering.
        return _not_found()
    return report
