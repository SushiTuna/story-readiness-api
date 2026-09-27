"""Error responses shared by the routers."""
from __future__ import annotations

import logging
import os

from fastapi.responses import JSONResponse

from app.linear_client import LinearError
from app.schemas import ErrorOut
from app.story_store import EVIDENCE_TYPES, MAX_EVIDENCE_BYTES, MAX_STORIES_PER_BOARD

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 10_000


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=ErrorOut(detail=detail).model_dump())


def is_too_large(description: str, ac: str) -> bool:
    """Return True if either field exceeds 10 000 chars."""
    return len(description) > MAX_TEXT_CHARS or len(ac) > MAX_TEXT_CHARS


def too_large(description: str, ac: str) -> JSONResponse | None:
    """Return a 413 JSONResponse if either field exceeds 10 000 chars, else None."""
    if is_too_large(description, ac):
        return _error(413, "The story is longer than the size limit.")
    return None


def board_not_found() -> JSONResponse:
    return _error(404, "The board was not found.")


def board_full() -> JSONResponse:
    return _error(409, f"The board already has {MAX_STORIES_PER_BOARD} stories. Delete one to make room.")


def evidence_invalid(message: str) -> JSONResponse:
    """422 in the same shape as a request validation error, so clients handle it like a missing blocked_reason."""
    error = {"loc": ["body", "done_evidence"], "msg": message, "type": "done_evidence_invalid"}
    return JSONResponse(status_code=422, content={"detail": [error]})


def status_not_allowed(message: str) -> JSONResponse:
    """422 for a move the story's verdict does not allow, in the same shape as a request validation error."""
    error = {"loc": ["body", "status"], "msg": message, "type": "status_not_allowed"}
    return JSONResponse(status_code=422, content={"detail": [error]})


def parent_invalid(message: str) -> JSONResponse:
    """422 for a split whose parent is not on the board, in the same shape as a request validation error."""
    error = {"loc": ["body", "parent_id"], "msg": message, "type": "parent_invalid"}
    return JSONResponse(status_code=422, content={"detail": [error]})


def blockers_open(message: str) -> JSONResponse:
    """422 for a move into in_sprint or done while the story waits on stories that are not done."""
    error = {"loc": ["body", "status"], "msg": message, "type": "blockers_open"}
    return JSONResponse(status_code=422, content={"detail": [error]})


def blocker_invalid(message: str, type_: str = "blocker_invalid") -> JSONResponse:
    """422 for a story to wait on that is not on the board (blocker_invalid), or that closes a cycle (dependency_cycle)."""
    error = {"loc": ["body", "blocker_id"], "msg": message, "type": type_}
    return JSONResponse(status_code=422, content={"detail": [error]})


def evidence_not_found() -> JSONResponse:
    return _error(404, "The evidence file was not found.")


def evidence_too_large() -> JSONResponse:
    return _error(413, f"The evidence file is larger than {MAX_EVIDENCE_BYTES // (1024 * 1024)} MB.")


def evidence_type_not_allowed() -> JSONResponse:
    types = " ".join(sorted(EVIDENCE_TYPES))
    return _error(415, f"The evidence file must be one of these types: {types}.")


def evidence_attached() -> JSONResponse:
    return _error(409, "The evidence file is part of a move to Done, so it is kept.")


_TRUE = {"1", "true", "yes", "on"}


def linear_enabled() -> bool:
    """The Linear integration is opt-in: off unless LINEAR_ENABLED is true, even when a token is set."""
    return os.getenv("LINEAR_ENABLED", "").strip().lower() in _TRUE


def linear_configured() -> bool:
    return linear_enabled() and bool(os.getenv("LINEAR_TOKEN"))


def source_not_configured() -> JSONResponse:
    return _error(409, "The source's credentials are not configured on the server.")


def linear_disabled() -> JSONResponse:
    return _error(409, "The Linear integration is disabled on the server. Set LINEAR_ENABLED=true to use it.")


def linear_not_configured() -> JSONResponse:
    """409 for a Linear request the server can't serve: the integration is off, or there is no token."""
    return source_not_configured() if linear_enabled() else linear_disabled()


def linear_error_response(exc: LinearError, *, not_found: str = "The issue was not found in the source.") -> JSONResponse:
    """Map a LinearError to 404 (not_found), 409 (auth) or 502 (anything else)."""
    logger.error("Linear error (%s): %s", exc.kind, exc.detail)
    if exc.kind == "not_found":
        return _error(404, not_found)
    if exc.kind == "auth":
        return _error(409, "The source's credentials were rejected. Check LINEAR_TOKEN on the server.")
    return _error(502, "The issue tracker is unavailable. Please try again later.")


def jev_unavailable() -> JSONResponse:
    return _error(502, "The assessment service is unavailable. Please try again later.")
