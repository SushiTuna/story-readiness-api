"""GET /sources — list configured integrations."""
from __future__ import annotations

import os

from fastapi import APIRouter

from app.schemas import SourceOut

router = APIRouter(tags=["sources"])


@router.get(
    "/sources",
    response_model=list[SourceOut],
    operation_id="listSources",
    summary="List configured source integrations",
)
async def list_sources() -> list[SourceOut]:
    return [
        SourceOut(
            name="jira",
            configured=bool(os.getenv("JIRA_TOKEN") and os.getenv("JIRA_BASE_URL")),
        ),
        SourceOut(
            name="linear",
            configured=bool(os.getenv("LINEAR_TOKEN")),
        ),
    ]
