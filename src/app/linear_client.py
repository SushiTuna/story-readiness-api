"""Linear GraphQL client."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Literal

import httpx

from app.schemas import CheckKind, ReportOut

logger = logging.getLogger(__name__)

LINEAR_API_URL = "https://api.linear.app/graphql"

# Every readiness comment starts with this heading; it is how our own comment is found again.
REPORT_HEADING = "## Story Readiness"

_VERDICT_TEXT = {
    "ready": "Ready",
    "discuss": "Discuss",
    "needs_refinement": "Needs refinement",
    "not_ready": "Not ready",
}

# Module-level singleton — created once on first use, reused across requests.
_client: httpx.AsyncClient | None = None


@dataclass
class LinearLabel:
    id: str
    name: str


@dataclass
class LinearIssue:
    id: str
    identifier: str
    title: str
    description: str
    url: str
    team_id: str = ""
    labels: list[LinearLabel] = field(default_factory=list)


@dataclass
class OwnComment:
    id: str
    body: str


class LinearError(Exception):
    """Wraps a Linear API error with a semantic kind."""

    def __init__(self, kind: Literal["not_found", "upstream", "auth"], detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def get_client() -> httpx.AsyncClient:
    """Return the module-level HTTP client, creating it on first call."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10.0)
    return _client


async def _graphql(query: str, variables: dict) -> dict:
    """POST a GraphQL request and return the parsed JSON body.

    Raises :class:`LinearError` with kind ``"upstream"`` on any transport
    error, non-JSON body, HTTP 5xx, or rate-limit response.  Raises with kind
    ``"auth"`` on HTTP 401 or an ``AUTHENTICATION_ERROR`` in the GraphQL errors.
    """
    token = os.getenv("LINEAR_TOKEN", "")
    client = get_client()
    try:
        resp = await client.post(
            LINEAR_API_URL,
            json={"query": query, "variables": variables},
            headers={
                "Authorization": token,
                "Content-Type": "application/json",
            },
        )
    except httpx.HTTPError as exc:
        raise LinearError("upstream", str(exc)) from exc

    # HTTP 401 — bad / missing token
    if resp.status_code == 401:
        raise LinearError("auth", "Authentication failed (HTTP 401)")

    # Rate-limit: HTTP 400 with extensions.code == "RATELIMITED"
    if resp.status_code == 400:
        try:
            body = resp.json()
        except Exception:
            raise LinearError("upstream", f"HTTP 400: {resp.text[:200]}")
        errors = body.get("errors", [])
        for err in errors:
            if err.get("extensions", {}).get("code") == "RATELIMITED":
                raise LinearError("upstream", "Linear rate limit exceeded")
        raise LinearError("upstream", f"HTTP 400: {resp.text[:200]}")

    if resp.status_code >= 500:
        raise LinearError("upstream", f"HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        body = resp.json()
    except Exception as exc:
        raise LinearError("upstream", "Non-JSON response from Linear") from exc

    # Check for AUTHENTICATION_ERROR in GraphQL-level errors before returning.
    errors = body.get("errors", [])
    for err in errors:
        if err.get("extensions", {}).get("code") == "AUTHENTICATION_ERROR":
            raise LinearError("auth", err.get("message", "Authentication error"))

    return body


async def fetch_issue(key: str) -> LinearIssue:
    """Fetch a Linear issue by its identifier (e.g. ``ENG-123``).

    Raises :class:`LinearError` with kind ``"not_found"`` when the issue does
    not exist, ``"auth"`` on authentication failures, or ``"upstream"`` on any
    other failure.
    """
    query = """
        query($id: String!) {
            issue(id: $id) {
                id
                identifier
                title
                description
                url
                team { id }
                labels { nodes { id name } }
            }
        }
    """
    body = await _graphql(query, {"id": key})

    errors = body.get("errors", [])
    if errors:
        # Linear returns INPUT_ERROR with userError=true and a message that
        # starts with "Entity not found" for an unknown identifier.
        # Only that specific pattern is a not_found; other user errors and
        # auth failures are handled above in _graphql.
        first = errors[0]
        ext = first.get("extensions", {})
        msg = first.get("message", "")
        if ext.get("code") == "INPUT_ERROR" and msg.startswith("Entity not found"):
            raise LinearError("not_found", f"Issue not found: {key}")
        raise LinearError("upstream", msg or "Unknown error")

    data = body.get("data", {})
    issue = data.get("issue") if data else None
    if issue is None:
        raise LinearError("not_found", f"Issue not found: {key}")

    raw_labels = (issue.get("labels") or {}).get("nodes") or []
    return LinearIssue(
        id=issue["id"],
        identifier=issue["identifier"],
        title=issue["title"],
        description=issue.get("description") or "",
        url=issue["url"],
        team_id=(issue.get("team") or {}).get("id") or "",
        labels=[LinearLabel(id=l["id"], name=l["name"]) for l in raw_labels],
    )


async def post_comment(issue_id: str, body: str) -> str:
    """Post a markdown comment on a Linear issue.

    Returns the new comment id.
    Raises :class:`LinearError` with kind ``"upstream"`` if the mutation
    reports failure.
    """
    mutation = """
        mutation($issueId: String!, $body: String!) {
            commentCreate(input: { issueId: $issueId, body: $body }) {
                success
                comment { id }
            }
        }
    """
    result = await _graphql(mutation, {"issueId": issue_id, "body": body})

    errors = result.get("errors", [])
    if errors:
        raise LinearError("upstream", errors[0].get("message", "commentCreate failed"))

    cc = (result.get("data") or {}).get("commentCreate", {})
    if not cc.get("success"):
        raise LinearError("upstream", "commentCreate returned success=false")

    return (cc.get("comment") or {}).get("id") or ""


async def find_own_comment(issue_id: str) -> OwnComment | None:
    """Return the viewer's existing readiness comment on *issue_id*, or None.

    Filters server-side on author (``isMe``) and the ``## Story Readiness`` heading,
    so the match does not depend on how many other comments the issue has.
    """
    query = """
        query($issueId: String!, $heading: String!) {
            issue(id: $issueId) {
                comments(
                    first: 1
                    orderBy: createdAt
                    filter: { user: { isMe: { eq: true } }, body: { startsWith: $heading } }
                ) {
                    nodes { id body }
                }
            }
        }
    """
    body = await _graphql(query, {"issueId": issue_id, "heading": REPORT_HEADING})
    errors = body.get("errors", [])
    if errors:
        raise LinearError("upstream", errors[0].get("message", "comment lookup failed"))

    nodes = (((body.get("data") or {}).get("issue") or {}).get("comments") or {}).get("nodes") or []
    if not nodes:
        return None
    return OwnComment(id=nodes[0]["id"], body=nodes[0].get("body") or "")


async def update_comment(comment_id: str, body: str) -> None:
    """Edit an existing Linear comment's body.

    Raises :class:`LinearError` with kind ``"upstream"`` if the mutation fails.
    """
    mutation = """
        mutation($id: String!, $body: String!) {
            commentUpdate(id: $id, input: { body: $body }) {
                success
            }
        }
    """
    result = await _graphql(mutation, {"id": comment_id, "body": body})
    errors = result.get("errors", [])
    if errors:
        raise LinearError("upstream", errors[0].get("message", "commentUpdate failed"))
    if not (result.get("data") or {}).get("commentUpdate", {}).get("success"):
        raise LinearError("upstream", "commentUpdate returned success=false")


async def upsert_report_comment(issue_id: str, body: str) -> None:
    """Update the viewer's existing readiness comment, or create a new one.

    Raises :class:`LinearError` on failure.
    """
    existing = await find_own_comment(issue_id)
    if existing:
        await update_comment(existing.id, body)
    else:
        await post_comment(issue_id, body)


async def write_report_comment(issue: LinearIssue, report: ReportOut) -> None:
    """Write *report* to the readiness comment on *issue*, showing what changed since the last run.

    Raises :class:`LinearError` on failure.
    """
    existing = await find_own_comment(issue.id)
    body = format_report_comment(
        report,
        previous_body=existing.body if existing else None,
        fingerprint=issue_fingerprint(issue.title, issue.description),
    )
    if existing:
        await update_comment(existing.id, body)
    else:
        await post_comment(issue.id, body)


async def _find_workspace_label(name: str, *, is_group: bool) -> dict | None:
    """Return ``{id, parent}`` for the workspace-level label named *name*, or None.

    Team labels are ignored: a team label cannot be applied to issues of other teams.
    """
    query = """
        query($name: String!, $isGroup: Boolean!) {
            issueLabels(filter: { name: { eq: $name }, isGroup: { eq: $isGroup }, team: { null: true } }) {
                nodes { id parent { id } }
            }
        }
    """
    body = await _graphql(query, {"name": name, "isGroup": is_group})
    errors = body.get("errors", [])
    if errors:
        raise LinearError("upstream", errors[0].get("message", "issueLabels query failed"))
    nodes = ((body.get("data") or {}).get("issueLabels") or {}).get("nodes") or []
    return nodes[0] if nodes else None


async def _create_label(name: str, *, is_group: bool = False, parent_id: str | None = None) -> str:
    mutation = """
        mutation($input: IssueLabelCreateInput!) {
            issueLabelCreate(input: $input) {
                success
                issueLabel { id }
            }
        }
    """
    label_input: dict = {"name": name, "color": "#6B7280"}
    if is_group:
        label_input["isGroup"] = True
    if parent_id:
        label_input["parentId"] = parent_id
    result = await _graphql(mutation, {"input": label_input})
    errors = result.get("errors", [])
    if errors:
        raise LinearError("upstream", errors[0].get("message", "issueLabelCreate failed"))
    created = (result.get("data") or {}).get("issueLabelCreate") or {}
    if not created.get("success"):
        raise LinearError("upstream", "issueLabelCreate returned success=false")
    return (created.get("issueLabel") or {}).get("id") or ""


async def _move_label(label_id: str, parent_id: str) -> None:
    mutation = """
        mutation($id: String!, $parentId: String!) {
            issueLabelUpdate(id: $id, input: { parentId: $parentId }) {
                success
            }
        }
    """
    result = await _graphql(mutation, {"id": label_id, "parentId": parent_id})
    errors = result.get("errors", [])
    if errors:
        raise LinearError("upstream", errors[0].get("message", "issueLabelUpdate failed"))
    if not ((result.get("data") or {}).get("issueLabelUpdate") or {}).get("success"):
        raise LinearError("upstream", "issueLabelUpdate returned success=false")


async def ensure_label_group(name: str) -> str:
    """Return the id of the workspace label group *name*, creating it if absent.

    Linear allows only one label from a group on an issue at a time.
    """
    group = await _find_workspace_label(name, is_group=True)
    if group:
        return group["id"]
    return await _create_label(name, is_group=True)


async def ensure_label(name: str, parent_id: str | None = None) -> str:
    """Return the id of the workspace label *name*, creating it if absent.

    With *parent_id*, the label is created in (or moved into) that label group.
    Raises :class:`LinearError` on failure.
    """
    label = await _find_workspace_label(name, is_group=False)
    if label is None:
        return await _create_label(name, parent_id=parent_id)
    if parent_id and (label.get("parent") or {}).get("id") != parent_id:
        await _move_label(label["id"], parent_id)
    return label["id"]


async def update_issue_labels(issue_id: str, add: list[str], remove: list[str]) -> None:
    """Apply *add* / *remove* label id lists to an issue in a single mutation.

    Raises :class:`LinearError` on failure.
    """
    mutation = """
        mutation($id: String!, $add: [String!], $remove: [String!]) {
            issueUpdate(id: $id, input: { addedLabelIds: $add, removedLabelIds: $remove }) {
                success
            }
        }
    """
    result = await _graphql(mutation, {"id": issue_id, "add": add, "remove": remove})
    errors = result.get("errors", [])
    if errors:
        raise LinearError("upstream", errors[0].get("message", "issueUpdate failed"))
    if not (result.get("data") or {}).get("issueUpdate", {}).get("success"):
        raise LinearError("upstream", "issueUpdate returned success=false")


def issue_fingerprint(title: str, description: str) -> str:
    """Short hash of the assessed text, stored in the comment to detect later edits."""
    return hashlib.sha256(f"{title}\n{description}".encode()).hexdigest()[:12]


_PREV_VERDICT = re.compile(re.escape(REPORT_HEADING) + r": \*\*(.+?)\*\*")
_PREV_QUALITY = re.compile(r"Quality score: \*\*(\d+)%\*\*")
_FINGERPRINT = re.compile(r"fingerprint: `([0-9a-f]{12})`")


def parse_fingerprint(comment_body: str) -> str | None:
    """Return the fingerprint stored in a readiness comment, or None for older comments."""
    match = _FINGERPRINT.search(comment_body)
    return match.group(1) if match else None


def _verdict_text(verdict: str) -> str:
    return _VERDICT_TEXT.get(verdict, verdict)


def _section_items(comment_body: str, heading: str) -> list[str]:
    """Return the ``- item`` lines under ``### heading`` in a previous comment."""
    items: list[str] = []
    in_section = False
    for line in comment_body.splitlines():
        if line.startswith("### "):
            in_section = line[4:].strip() == heading
            continue
        if in_section and line.startswith("- "):
            items.append(line[2:].strip())
    return items


def format_report_comment(
    report: ReportOut,
    *,
    previous_body: str | None = None,
    fingerprint: str | None = None,
) -> str:
    """Format a :class:`ReportOut` as a markdown comment for Linear.

    *previous_body* is the comment being replaced; when given, the comment shows
    what changed since the last assessment.
    """
    id_to_label = {c.id: c.label for c in report.checks}
    blockers = [id_to_label.get(b, b) for b in report.blockers_failed]
    discuss = [id_to_label.get(t, t) for t in report.to_discuss]
    verdict = _verdict_text(report.verdict)
    quality = f"{report.quality:.0%}"

    lines: list[str] = [f"{REPORT_HEADING}: **{verdict}**", ""]

    prev_quality = prev_verdict = None
    if previous_body:
        if match := _PREV_QUALITY.search(previous_body):
            prev_quality = f"{match.group(1)}%"
        if match := _PREV_VERDICT.search(previous_body):
            prev_verdict = _verdict_text(match.group(1))

    if prev_quality and prev_quality != quality:
        lines.append(f"Quality score: **{quality}** (was {prev_quality})")
    else:
        lines.append(f"Quality score: **{quality}**")
    if prev_verdict and prev_verdict != verdict:
        lines += ["", f"Verdict changed from **{prev_verdict}**."]

    if previous_body:
        still_open = set(blockers) | set(discuss)
        resolved = [
            item
            for item in _section_items(previous_body, "Blockers failed")
            + _section_items(previous_body, "Items to discuss")
            if item not in still_open
        ]
        if resolved:
            lines += ["", "### Resolved since last assessment", ""]
            lines += [f"- {item}" for item in dict.fromkeys(resolved)]

    if blockers:
        lines += ["", "### Blockers failed", ""]
        lines += [f"- {b}" for b in blockers]

    if discuss:
        lines += ["", "### Items to discuss", ""]
        lines += [f"- {t}" for t in discuss]

    # Questions only for checks that need action. Flags are display-only, so a
    # failing flag becomes a note rather than a question.
    questions: list[str] = []
    notes: list[str] = []
    for check in report.checks:
        if check.kind == CheckKind.flag:
            if not check.passed:
                notes.extend(check.ask)
        elif not check.passed or check.unsure:
            questions.extend(check.ask)

    if questions:
        lines += ["", "### Questions for the author", ""]
        lines += [f"- {q}" for q in questions]

    if notes:
        lines += ["", "### Notes", ""]
        lines += [f"- {n}" for n in notes]

    # Footer for traceability; the fingerprint lets the webhook detect later edits.
    footer_parts: list[str] = []
    if report.jev_model:
        footer_parts.append(f"model: `{report.jev_model}`")
    if report.request_id:
        footer_parts.append(f"request: `{report.request_id}`")
    if fingerprint:
        footer_parts.append(f"fingerprint: `{fingerprint}`")
    if footer_parts:
        lines += ["", "---", f"*{' · '.join(footer_parts)}*"]

    return "\n".join(lines)
