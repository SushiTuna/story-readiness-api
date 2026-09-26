"""Shared Linear → Jev assessment logic used by both the pull route and the webhook worker."""

from __future__ import annotations

from app import engine
from app.linear_client import LinearIssue
from app.schemas import AssessRequest, ReportOut

# Labels this service manages itself; they say nothing about the story, so they are not hints.
SERVICE_LABEL_PREFIX = "readiness:"


def label_hints(issue: LinearIssue, trigger_label: str | None = None) -> list[str]:
    """Return the issue's labels that are useful as story-type hints."""
    return [
        label.name
        for label in issue.labels
        if not label.name.startswith(SERVICE_LABEL_PREFIX) and label.name != trigger_label
    ]


async def assess_linear_issue(
    issue: LinearIssue,
    definition_of_ready: list[str] | None = None,
    trigger_label: str | None = None,
) -> ReportOut:
    """Build an :class:`AssessRequest` from *issue* and run the engine.

    Raises :class:`app.jev_client.JevError` on Jev failures (caller decides how
    to surface them).  Does **not** touch Linear (no comments, no labels).
    """
    req = AssessRequest(
        title=issue.title[:500],
        description=issue.description,
        acceptance_criteria="",
        definition_of_ready=definition_of_ready or [],
    )
    return await engine.assess(
        req,
        source="linear",
        key=issue.identifier,
        url=issue.url,
        labels=label_hints(issue, trigger_label) or None,
    )
