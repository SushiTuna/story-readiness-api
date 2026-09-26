"""Boards: each one is scoped to a single goal and holds up to MAX_STORIES_PER_BOARD stories.

The stories on a board are listed and created in routers/stories.py. Handlers are
plain ``def`` so FastAPI runs the blocking sqlite3 calls in its threadpool.
"""
from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from app import story_store
from app.routers.errors import board_not_found
from app.schemas import BoardIn, BoardOut, BoardUpdateIn, ErrorOut
from app.story_store import Board

router = APIRouter(tags=["boards"])

_NOT_FOUND = {404: {"model": ErrorOut, "description": "The board was not found."}}
_PREFIX_TAKEN = {409: {"model": ErrorOut, "description": "Another board already uses this key prefix."}}


def _out(board: Board) -> BoardOut:
    return BoardOut(
        id=board.id,
        name=board.name,
        description=board.description,
        key_prefix=board.key_prefix,
        created_at=board.created_at,
        story_count=board.story_count,
        story_limit=story_store.MAX_STORIES_PER_BOARD,
    )


@router.get(
    "/boards",
    response_model=list[BoardOut],
    operation_id="listBoards",
    summary="List boards with their story counts",
)
def list_boards() -> list[BoardOut]:
    return [_out(board) for board in story_store.list_boards()]


@router.post(
    "/boards",
    response_model=BoardOut,
    status_code=201,
    operation_id="createBoard",
    summary="Create an empty board",
    responses=_PREFIX_TAKEN,
)
def create_board(body: BoardIn) -> BoardOut | JSONResponse:
    try:
        board = story_store.create_board(name=body.name, key_prefix=body.key_prefix, description=body.description)
    except story_store.DuplicateKeyPrefixError:
        return JSONResponse(
            status_code=409,
            content=ErrorOut(detail=f"Another board already uses the key prefix {body.key_prefix}.").model_dump(),
        )
    return _out(board)


@router.put(
    "/boards/{board_id}",
    response_model=BoardOut,
    operation_id="updateBoard",
    summary="Rename a board or change its description",
    description="The key prefix cannot change, so story keys stay stable.",
    responses=_NOT_FOUND,
)
def update_board(board_id: str, body: BoardUpdateIn) -> BoardOut | JSONResponse:
    board = story_store.update_board(board_id, name=body.name, description=body.description)
    if board is None:
        return board_not_found()
    return _out(board)


@router.delete(
    "/boards/{board_id}",
    status_code=204,
    response_class=Response,
    operation_id="deleteBoard",
    summary="Delete a board with all its stories and their assessment history",
    responses=_NOT_FOUND,
)
def delete_board(board_id: str) -> Response:
    if not story_store.delete_board(board_id):
        return board_not_found()
    return Response(status_code=204)
