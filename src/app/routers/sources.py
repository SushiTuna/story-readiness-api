"""GET /sources — list configured integrations, and read-only Linear listings for the board."""
from __future__ import annotations

import os

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.linear_assess import SERVICE_LABEL_PREFIX
from app.linear_client import LinearError, LinearIssue, list_team_issues, list_teams
from app.routers.errors import linear_configured, linear_error_response, linear_not_configured
from app.schemas import ErrorOut, IssueCardOut, LinearTeamOut, SourceOut

router = APIRouter(tags=["sources"])

_LINEAR_ERRORS = {
    409: {"model": ErrorOut, "description": "The source is disabled, or its credentials are missing or were rejected."},
    502: {"model": ErrorOut, "description": "The upstream service is unavailable."},
}


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
            configured=linear_configured(),
        ),
    ]


@router.get(
    "/sources/linear/teams",
    response_model=list[LinearTeamOut],
    operation_id="listLinearTeams",
    summary="List the Linear teams the token can see",
    responses=_LINEAR_ERRORS,
)
async def list_linear_teams() -> list[LinearTeamOut] | JSONResponse:
    if not linear_configured():
        return linear_not_configured()
    try:
        teams = await list_teams()
    except LinearError as exc:
        return linear_error_response(exc)
    return [LinearTeamOut(id=t.id, key=t.key, name=t.name) for t in teams]


def _card(issue: LinearIssue) -> IssueCardOut:
    names = [label.name for label in issue.labels]
    readiness = next(
        (n.removeprefix(SERVICE_LABEL_PREFIX) for n in names if n.startswith(SERVICE_LABEL_PREFIX)),
        None,
    )
    return IssueCardOut(
        key=issue.identifier,
        title=issue.title,
        description=issue.description,
        url=issue.url,
        labels=names,
        state_type=issue.state_type,
        readiness=readiness,
    )


@router.get(
    "/sources/linear/issues",
    response_model=list[IssueCardOut],
    operation_id="listLinearIssues",
    summary="List a Linear team's open issues",
    description="Read-only. Returns up to 100 issues that are not completed or canceled, most recently updated first.",
    responses={
        404: {"model": ErrorOut, "description": "The team was not found in the source."},
        **_LINEAR_ERRORS,
    },
)
async def list_linear_issues(
    team: str = Query(min_length=1, max_length=100, description="Linear team id, from /sources/linear/teams."),
) -> list[IssueCardOut] | JSONResponse:
    if not linear_configured():
        return linear_not_configured()
    try:
        issues = await list_team_issues(team)
    except LinearError as exc:
        return linear_error_response(exc, not_found="The team was not found in the source.")
    return [_card(issue) for issue in issues]
