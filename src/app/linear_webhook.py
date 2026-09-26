"""Linear webhook: signature verification, replay protection, and issue processing."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.jev_client import JevError
from app.linear_assess import SERVICE_LABEL_PREFIX, assess_linear_issue
from app.linear_client import (
    REPORT_HEADING,
    LinearError,
    LinearIssue,
    ensure_label,
    ensure_label_group,
    fetch_issue,
    find_own_comment,
    issue_fingerprint,
    parse_fingerprint,
    update_issue_labels,
    upsert_report_comment,
    write_report_comment,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

TRIGGER_LABEL_DEFAULT = "ready-check"
VERDICT_LABEL_PREFIX = SERVICE_LABEL_PREFIX
# Workspace label group holding the readiness:* labels; Linear allows one label per group.
VERDICT_LABEL_GROUP = "Readiness"
STALE_LABEL = f"{VERDICT_LABEL_PREFIX}stale"
ERROR_LABEL = f"{VERDICT_LABEL_PREFIX}error"
_MAX_DESCRIPTION_CHARS = 10_000


def _trigger_label() -> str:
    return os.getenv("LINEAR_TRIGGER_LABEL", TRIGGER_LABEL_DEFAULT)


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def verify_signature(raw_body: bytes, header: str | None, secret: str) -> bool:
    """Return True when *header* is a valid HMAC-SHA256 of *raw_body* with *secret*."""
    if not header:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def is_fresh(ts_ms: int | float, *, window_s: int = 60) -> bool:
    """Return True when *ts_ms* (milliseconds) is within *window_s* of now."""
    age_s = abs(time.time() - ts_ms / 1000.0)
    return age_s <= window_s


# ---------------------------------------------------------------------------
# Delivery dedupe cache (in-memory TTL set, single-process)
# ---------------------------------------------------------------------------

_SEEN_MAX = 1_000
# Covers repeats that still pass the 60 s freshness window; a delivery older than
# that is already rejected by is_fresh, so a longer TTL would not add anything.
_SEEN_TTL_S = 300

# OrderedDict as an ordered set: key = delivery_id, value = expiry timestamp
_seen: OrderedDict[str, float] = OrderedDict()


def _is_duplicate(delivery_id: str) -> bool:
    """Return True and record *delivery_id* if it was already processed recently."""
    now = time.time()
    # Evict expired entries (oldest first)
    while _seen and next(iter(_seen.values())) < now:
        _seen.popitem(last=False)
    # Enforce size cap
    while len(_seen) >= _SEEN_MAX:
        _seen.popitem(last=False)

    if delivery_id in _seen:
        return True
    _seen[delivery_id] = now + _SEEN_TTL_S
    return False


# ---------------------------------------------------------------------------
# Per-issue processing lock
# ---------------------------------------------------------------------------

# issue_id → (lock, number of tasks holding or waiting for it); entries are
# dropped when the last task leaves so the dict does not grow without bound.
_issue_locks: dict[str, tuple[asyncio.Lock, int]] = {}


@asynccontextmanager
async def _issue_lock(issue_id: str) -> AsyncIterator[None]:
    lock, users = _issue_locks.get(issue_id, (asyncio.Lock(), 0))
    _issue_locks[issue_id] = (lock, users + 1)
    try:
        async with lock:
            yield
    finally:
        lock, users = _issue_locks[issue_id]
        if users <= 1:
            del _issue_locks[issue_id]
        else:
            _issue_locks[issue_id] = (lock, users - 1)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

async def process_issue(issue_id: str) -> None:
    """Handle an Issue event for *issue_id*.

    - Trigger label present → assess and write the comment + verdict label.
    - A verdict label present and the title/description changed since the
      assessed version → swap the verdict label for ``readiness:stale``.
    - Otherwise nothing.
    """
    async with _issue_lock(issue_id):
        try:
            issue = await fetch_issue(issue_id)
        except LinearError as exc:
            logger.error("Webhook: fetch_issue(%s) failed (%s): %s", issue_id, exc.kind, exc.detail)
            return

        trigger = _trigger_label()
        if any(label.name == trigger for label in issue.labels):
            await _assess_and_publish(issue, trigger)
        elif any(
            label.name.startswith(VERDICT_LABEL_PREFIX) and label.name not in (STALE_LABEL, ERROR_LABEL)
            for label in issue.labels
        ):
            await _mark_stale_if_changed(issue)
        else:
            logger.debug("Webhook: trigger label absent on %s — skipping", issue.identifier)


async def _set_verdict_label(issue: LinearIssue, name: str, *, also_remove: list[str] = ()) -> None:
    """Replace the issue's readiness:* label with *name* in one issueUpdate."""
    group_id = await ensure_label_group(VERDICT_LABEL_GROUP)
    label_id = await ensure_label(name, parent_id=group_id)
    remove_ids = [
        label.id for label in issue.labels
        if label.name.startswith(VERDICT_LABEL_PREFIX) and label.id != label_id
    ] + list(also_remove)
    add_ids = [] if any(label.id == label_id for label in issue.labels) else [label_id]
    await update_issue_labels(issue.id, add=add_ids, remove=remove_ids)


async def _assess_and_publish(issue: LinearIssue, trigger: str) -> None:
    trigger_ids = [label.id for label in issue.labels if label.name == trigger]

    if len(issue.description) > _MAX_DESCRIPTION_CHARS:
        logger.warning("Webhook: %s description too large (%d chars)", issue.identifier, len(issue.description))
        await _report_problem(
            issue,
            trigger_ids,
            f"The issue description exceeds the {_MAX_DESCRIPTION_CHARS:,}-character limit "
            "and cannot be assessed automatically.",
        )
        return

    try:
        report = await assess_linear_issue(issue, trigger_label=trigger)
    except JevError as exc:
        logger.error("Webhook: Jev error on %s: %s", issue.identifier, exc.detail)
        await _report_problem(issue, trigger_ids, "The assessment service failed.")
        return

    # A failed Linear write keeps the trigger label, so the next issue event retries.
    try:
        await write_report_comment(issue, report)
        await _set_verdict_label(issue, f"{VERDICT_LABEL_PREFIX}{report.verdict}", also_remove=trigger_ids)
    except LinearError as exc:
        logger.error("Webhook: writing results to %s failed: %s", issue.identifier, exc.detail)
        return

    logger.info("Webhook: assessed %s → %s", issue.identifier, report.verdict)


async def _report_problem(issue: LinearIssue, trigger_ids: list[str], reason: str) -> None:
    """Tell the author in Linear that the assessment did not run.

    The trigger label is removed so the failure cannot re-run on every later
    edit; re-adding it retries.
    """
    trigger = _trigger_label()
    body = (
        f"{REPORT_HEADING}: **Not assessed**\n\n{reason}\n\n"
        f"Remove and re-add the `{trigger}` label to try again."
    )
    try:
        await upsert_report_comment(issue.id, body)
        await _set_verdict_label(issue, ERROR_LABEL, also_remove=trigger_ids)
    except LinearError as exc:
        logger.error("Webhook: could not report failure on %s: %s", issue.identifier, exc.detail)


async def _mark_stale_if_changed(issue: LinearIssue) -> None:
    """Swap the verdict label for readiness:stale when the assessed text has changed."""
    try:
        comment = await find_own_comment(issue.id)
    except LinearError as exc:
        logger.error("Webhook: comment lookup on %s failed: %s", issue.identifier, exc.detail)
        return
    if comment is None:
        return
    assessed = parse_fingerprint(comment.body)
    if assessed is None or assessed == issue_fingerprint(issue.title, issue.description):
        return  # older comment without a fingerprint, or nothing changed

    try:
        await _set_verdict_label(issue, STALE_LABEL)
    except LinearError as exc:
        logger.error("Webhook: marking %s stale failed: %s", issue.identifier, exc.detail)
        return
    logger.info("Webhook: %s changed since its assessment → %s", issue.identifier, STALE_LABEL)
