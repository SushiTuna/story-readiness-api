"""Tests for the Linear webhook receiver and process_issue worker."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import app.linear_webhook as lw
import app.linear_client as lc
from app.main import app
from app.linear_client import LinearError, LinearIssue, LinearLabel
from app.schemas import (
    CheckKind,
    CheckOut,
    ReportOut,
    StoryOut,
    StorySource,
    StoryType,
    StoryTypeChoice,
    VerdictEnum,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SECRET = "test-webhook-secret"


def _sign(body: bytes, secret: str = _SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _make_payload(
    *,
    issue_id: str = "issue-uuid-1",
    action: str = "update",
    event_type: str = "Issue",
    ts_ms: int | None = None,
) -> dict:
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    return {
        "type": event_type,
        "action": action,
        "data": {"id": issue_id},
        "webhookTimestamp": ts_ms,
    }


def _make_issue(
    *,
    issue_id: str = "issue-uuid-1",
    identifier: str = "CJD-1",
    trigger_label: bool = True,
    labels: list[LinearLabel] | None = None,
) -> LinearIssue:
    if labels is None:
        labels = [LinearLabel(id="lbl-trigger", name="ready-check")] if trigger_label else []
    return LinearIssue(
        id=issue_id,
        identifier=identifier,
        title="Test issue",
        description="As a user I want to do something",
        url="https://linear.app/cjd/issue/CJD-1",
        team_id="team-1",
        labels=labels,
    )


def _make_report(verdict: str = "not_ready") -> ReportOut:
    return ReportOut(
        verdict=VerdictEnum(verdict),
        quality=0.5,
        checks=[
            CheckOut(
                id="ac_present",
                label="Acceptance criteria present",
                kind=CheckKind.blocker,
                value=0.0,
                passed=False,
                unsure=False,
                answer={"present": 0},
                ask=["What are the acceptance criteria?"],
            ),
        ],
        story=StoryOut(
            title="Test issue",
            description="As a user I want to do something",
            acceptance_criteria="",
            key="CJD-1",
            source=StorySource.linear,
            url="https://linear.app/cjd/issue/CJD-1",
        ),
        story_type=StoryType(
            choice=StoryTypeChoice.user_feature,
            confidence=0.9,
            probabilities={"user_feature": 0.9, "technical": 0.07, "bug": 0.03},
        ),
        blockers_failed=["ac_present"],
        to_discuss=[],
        jev_model="jev-1",
        request_id="req-abc",
        latency_ms=100,
        input_tokens=256,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture(autouse=True)
def reset_seen_cache():
    """Clear the delivery dedupe cache before each test."""
    lw._seen.clear()
    yield
    lw._seen.clear()


# ---------------------------------------------------------------------------
# Receiver route — signature and timestamp checks
# ---------------------------------------------------------------------------

async def test_webhook_disabled_returns_409_and_schedules_nothing(client, monkeypatch):
    """LINEAR_ENABLED off → 409 even for a correctly signed delivery; the issue is not processed."""
    monkeypatch.setenv("LINEAR_ENABLED", "false")
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)
    called = []

    async def fake_process(issue_id):
        called.append(issue_id)

    monkeypatch.setattr("app.routers.webhooks.process_issue", fake_process)
    body = json.dumps(_make_payload()).encode()
    resp = await client.post(
        "/api/webhooks/linear",
        content=body,
        headers={"Content-Type": "application/json", "Linear-Signature": _sign(body)},
    )
    assert resp.status_code == 409
    assert "disabled" in resp.json()["detail"]
    assert called == []


async def test_webhook_no_secret_returns_409(client, monkeypatch):
    """Missing LINEAR_WEBHOOK_SECRET → 409."""
    monkeypatch.delenv("LINEAR_WEBHOOK_SECRET", raising=False)
    body = json.dumps(_make_payload()).encode()
    resp = await client.post(
        "/api/webhooks/linear",
        content=body,
        headers={"Content-Type": "application/json", "Linear-Signature": _sign(body)},
    )
    assert resp.status_code == 409


async def test_webhook_missing_signature_returns_401(client, monkeypatch):
    """No Linear-Signature header → 401."""
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)
    body = json.dumps(_make_payload()).encode()
    resp = await client.post(
        "/api/webhooks/linear",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


async def test_webhook_invalid_signature_returns_401(client, monkeypatch):
    """Tampered body → 401."""
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)
    body = json.dumps(_make_payload()).encode()
    tampered = body + b"x"
    resp = await client.post(
        "/api/webhooks/linear",
        content=tampered,
        headers={
            "Content-Type": "application/json",
            "Linear-Signature": _sign(body),  # sig is for original, not tampered
        },
    )
    assert resp.status_code == 401


async def test_webhook_wrong_secret_returns_401(client, monkeypatch):
    """Signature computed with a different secret → 401."""
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)
    body = json.dumps(_make_payload()).encode()
    resp = await client.post(
        "/api/webhooks/linear",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Linear-Signature": _sign(body, secret="wrong-secret"),
        },
    )
    assert resp.status_code == 401


async def test_webhook_stale_timestamp_returns_401(client, monkeypatch):
    """Timestamp older than 60 s → 401."""
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)
    stale_ts = int((time.time() - 120) * 1000)
    body = json.dumps(_make_payload(ts_ms=stale_ts)).encode()
    resp = await client.post(
        "/api/webhooks/linear",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Linear-Signature": _sign(body),
        },
    )
    assert resp.status_code == 401


async def test_webhook_valid_returns_200_immediately(client, monkeypatch):
    """Valid signature + fresh timestamp + Issue event → 200 without waiting for background."""
    import app.routers.webhooks as wh_router

    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)

    async def _noop(issue_id):
        pass

    # Patch the router's binding so the background task is a no-op.
    monkeypatch.setattr(wh_router, "process_issue", _noop)

    body = json.dumps(_make_payload()).encode()
    resp = await client.post(
        "/api/webhooks/linear",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Linear-Signature": _sign(body),
            "Linear-Delivery": "delivery-uuid-1",
        },
    )
    assert resp.status_code == 200


async def test_webhook_non_issue_event_ignored(client, monkeypatch):
    """type='Comment' events → 200 and process_issue is NOT called."""
    import app.routers.webhooks as wh_router

    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)
    called = []

    async def _track(issue_id):
        called.append(issue_id)

    monkeypatch.setattr(wh_router, "process_issue", _track)

    payload = _make_payload(event_type="Comment")
    body = json.dumps(payload).encode()
    resp = await client.post(
        "/api/webhooks/linear",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Linear-Signature": _sign(body),
        },
    )
    assert resp.status_code == 200
    assert not called


async def test_webhook_duplicate_delivery_ignored(client, monkeypatch):
    """The same Linear-Delivery id is only processed once."""
    import app.routers.webhooks as wh_router

    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)
    called = []

    async def _mock_process(issue_id):
        called.append(issue_id)

    # The router imports process_issue by name; patch the router's binding.
    monkeypatch.setattr(wh_router, "process_issue", _mock_process)

    body = json.dumps(_make_payload()).encode()
    headers = {
        "Content-Type": "application/json",
        "Linear-Signature": _sign(body),
        "Linear-Delivery": "dup-delivery-uuid",
    }
    await client.post("/api/webhooks/linear", content=body, headers=headers)
    # Second delivery with the same id — should be ignored
    resp = await client.post("/api/webhooks/linear", content=body, headers=headers)
    assert resp.status_code == 200
    assert len(called) == 1  # only one background task scheduled


# ---------------------------------------------------------------------------
# Receiver route — malformed but correctly signed payloads, capture
# ---------------------------------------------------------------------------

async def _post_signed(client, raw: bytes, **headers):
    return await client.post(
        "/api/webhooks/linear",
        content=raw,
        headers={"Content-Type": "application/json", "Linear-Signature": _sign(raw), **headers},
    )


@pytest.mark.parametrize("raw", [
    b"not json",
    b"[]",
    json.dumps({"type": "Issue", "action": "update", "data": {"id": "x"}, "webhookTimestamp": "abc"}).encode(),
    json.dumps({"type": "Issue", "action": "update", "data": {"id": "x"}, "webhookTimestamp": True}).encode(),
])
async def test_webhook_malformed_signed_payload_returns_400(client, monkeypatch, raw):
    """A validly signed but malformed body → 400, never a 500."""
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)
    resp = await _post_signed(client, raw)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid webhook payload."


async def test_webhook_non_dict_data_is_ignored(client, monkeypatch):
    """An Issue event whose data is not an object → 200, nothing scheduled."""
    import app.routers.webhooks as wh_router

    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)
    called = []

    async def _track(issue_id):
        called.append(issue_id)

    monkeypatch.setattr(wh_router, "process_issue", _track)
    payload = {**_make_payload(), "data": ["not", "an", "object"]}
    resp = await _post_signed(client, json.dumps(payload).encode())
    assert resp.status_code == 200
    assert not called


async def test_webhook_capture_writes_delivery(client, monkeypatch, tmp_path):
    """With LINEAR_WEBHOOK_CAPTURE_DIR set, a verified delivery is saved without its signature."""
    import app.routers.webhooks as wh_router

    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setenv("LINEAR_WEBHOOK_CAPTURE_DIR", str(tmp_path))

    async def _noop(issue_id):
        pass

    monkeypatch.setattr(wh_router, "process_issue", _noop)
    payload = _make_payload()
    resp = await _post_signed(client, json.dumps(payload).encode(), **{"Linear-Delivery": "cap-1"})

    assert resp.status_code == 200
    saved = json.loads((tmp_path / "cap-1.json").read_text())
    assert saved["payload"] == payload
    assert saved["headers"]["Linear-Delivery"] == "cap-1"
    assert "Linear-Signature" not in saved["headers"]


# ---------------------------------------------------------------------------
# process_issue worker
# ---------------------------------------------------------------------------

class _LinearRecorder:
    """Records the Linear writes process_issue makes."""

    def __init__(self, monkeypatch, issue: LinearIssue, *, comment: lc.OwnComment | None = None):
        self.issue = issue
        self.comment = comment
        self.reports: list = []
        self.upserts: list[str] = []
        self.label_updates: list[tuple[list[str], list[str]]] = []
        self.ensured: list[tuple[str, str | None]] = []

        async def _fetch(key):
            return self.issue

        async def _write(issue, report):
            self.reports.append(report)

        async def _upsert(issue_id, body):
            self.upserts.append(body)

        async def _find(issue_id):
            return self.comment

        async def _group(name):
            return "grp-readiness"

        async def _ensure(name, parent_id=None):
            self.ensured.append((name, parent_id))
            return f"lbl-{name}"

        async def _update(issue_id, add, remove):
            self.label_updates.append((add, remove))

        monkeypatch.setattr(lw, "fetch_issue", _fetch)
        monkeypatch.setattr(lw, "write_report_comment", _write)
        monkeypatch.setattr(lw, "upsert_report_comment", _upsert)
        monkeypatch.setattr(lw, "find_own_comment", _find)
        monkeypatch.setattr(lw, "ensure_label_group", _group)
        monkeypatch.setattr(lw, "ensure_label", _ensure)
        monkeypatch.setattr(lw, "update_issue_labels", _update)


async def test_process_issue_no_trigger_label_is_noop(monkeypatch):
    """No trigger and no verdict label → no Linear writes."""
    rec = _LinearRecorder(monkeypatch, _make_issue(trigger_label=False))

    await lw.process_issue("issue-uuid-1")

    assert not rec.reports and not rec.upserts and not rec.label_updates


async def test_process_issue_happy_path(monkeypatch):
    """Trigger label present → comment written, verdict label (in group) added, trigger and old verdict removed."""
    issue = _make_issue(labels=[
        LinearLabel(id="lbl-trigger", name="ready-check"),
        LinearLabel(id="lbl-old", name="readiness:ready"),
        LinearLabel(id="lbl-bug", name="Bug"),
    ])
    rec = _LinearRecorder(monkeypatch, issue)
    report = _make_report("not_ready")
    seen: dict = {}

    async def _mock_assess(iss, trigger_label=None):
        seen["trigger_label"] = trigger_label
        return report

    monkeypatch.setattr(lw, "assess_linear_issue", _mock_assess)

    await lw.process_issue("issue-uuid-1")

    assert rec.reports == [report]
    assert seen["trigger_label"] == "ready-check"
    assert rec.ensured == [("readiness:not_ready", "grp-readiness")]
    add, remove = rec.label_updates[0]
    assert add == ["lbl-readiness:not_ready"]
    assert set(remove) == {"lbl-old", "lbl-trigger"}  # Bug is untouched


async def test_process_issue_jev_error_reports_in_linear(monkeypatch):
    """A JevError → 'Not assessed' comment, readiness:error label, trigger removed (no retry loop)."""
    from app.jev_client import JevError

    rec = _LinearRecorder(monkeypatch, _make_issue())

    async def _raise_jev(iss, trigger_label=None):
        raise JevError(status_code=502, detail="Jev down")

    monkeypatch.setattr(lw, "assess_linear_issue", _raise_jev)

    await lw.process_issue("issue-uuid-1")  # must not raise

    assert len(rec.upserts) == 1
    assert "**Not assessed**" in rec.upserts[0]
    assert "re-add the `ready-check` label" in rec.upserts[0]
    assert rec.ensured == [("readiness:error", "grp-readiness")]
    add, remove = rec.label_updates[0]
    assert add == ["lbl-readiness:error"]
    assert "lbl-trigger" in remove


async def test_process_issue_linear_write_failure_keeps_trigger(monkeypatch):
    """If writing the comment fails, labels are untouched so the next event retries."""
    rec = _LinearRecorder(monkeypatch, _make_issue())

    async def _mock_assess(iss, trigger_label=None):
        return _make_report()

    async def _write_fails(issue, report):
        raise LinearError("upstream", "boom")

    monkeypatch.setattr(lw, "assess_linear_issue", _mock_assess)
    monkeypatch.setattr(lw, "write_report_comment", _write_fails)

    await lw.process_issue("issue-uuid-1")

    assert not rec.label_updates


async def test_process_issue_oversized_description(monkeypatch):
    """Description > 10 000 chars → no Jev call, 'Not assessed' comment, error label, trigger removed."""
    issue = _make_issue()
    issue.description = "x" * 10_001
    rec = _LinearRecorder(monkeypatch, issue)
    assess_called = []

    async def _mock_assess(iss, trigger_label=None):
        assess_called.append(True)

    monkeypatch.setattr(lw, "assess_linear_issue", _mock_assess)

    await lw.process_issue("issue-uuid-1")

    assert not assess_called
    assert "limit" in rec.upserts[0].lower()
    add, remove = rec.label_updates[0]
    assert add == ["lbl-readiness:error"]
    assert "lbl-trigger" in remove


def _assessed_issue(description: str) -> LinearIssue:
    issue = _make_issue(labels=[LinearLabel(id="lbl-ready", name="readiness:ready")])
    issue.description = description
    return issue


def _comment_for(issue: LinearIssue, text: str) -> lc.OwnComment:
    fp = lc.issue_fingerprint(issue.title, text)
    return lc.OwnComment(id="c-1", body=f"## Story Readiness: **Ready**\n\n---\n*fingerprint: `{fp}`*")


async def test_process_issue_marks_stale_after_edit(monkeypatch):
    """Verdict label present and text changed since assessment → readiness:stale replaces it."""
    issue = _assessed_issue("edited text")
    rec = _LinearRecorder(monkeypatch, issue, comment=_comment_for(issue, "original text"))

    await lw.process_issue("issue-uuid-1")

    assert rec.ensured == [("readiness:stale", "grp-readiness")]
    add, remove = rec.label_updates[0]
    assert add == ["lbl-readiness:stale"]
    assert remove == ["lbl-ready"]
    assert not rec.reports  # no Jev call


async def test_process_issue_unchanged_text_is_not_stale(monkeypatch):
    """Our own label update (text unchanged) must not mark the issue stale."""
    issue = _assessed_issue("same text")
    rec = _LinearRecorder(monkeypatch, issue, comment=_comment_for(issue, "same text"))

    await lw.process_issue("issue-uuid-1")

    assert not rec.label_updates


async def test_process_issue_old_comment_without_fingerprint_is_left_alone(monkeypatch):
    """Comments written before fingerprints existed can't be compared → no change."""
    issue = _assessed_issue("any text")
    old = lc.OwnComment(id="c-1", body="## Story Readiness: **ready**\n\nQuality score: **94%**")
    rec = _LinearRecorder(monkeypatch, issue, comment=old)

    await lw.process_issue("issue-uuid-1")

    assert not rec.label_updates


async def test_process_issue_stale_label_is_not_rechecked(monkeypatch):
    """An issue already marked stale needs no comment lookup."""
    issue = _make_issue(labels=[LinearLabel(id="lbl-stale", name="readiness:stale")])
    rec = _LinearRecorder(monkeypatch, issue)
    lookups = []

    async def _find(issue_id):
        lookups.append(issue_id)

    monkeypatch.setattr(lw, "find_own_comment", _find)

    await lw.process_issue("issue-uuid-1")

    assert not lookups and not rec.label_updates


async def test_process_issue_releases_lock_entry(monkeypatch):
    """The per-issue lock entry is removed once processing finishes."""
    _LinearRecorder(monkeypatch, _make_issue(trigger_label=False))

    await asyncio.gather(lw.process_issue("issue-uuid-1"), lw.process_issue("issue-uuid-1"))

    assert "issue-uuid-1" not in lw._issue_locks


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def test_verify_signature_valid():
    body = b'{"type":"Issue"}'
    sig = _sign(body)
    assert lw.verify_signature(body, sig, _SECRET)


def test_verify_signature_wrong_body():
    body = b'{"type":"Issue"}'
    sig = _sign(body)
    assert not lw.verify_signature(b'tampered', sig, _SECRET)


def test_verify_signature_missing_header():
    assert not lw.verify_signature(b'body', None, _SECRET)


def test_is_fresh_recent():
    ts_ms = int(time.time() * 1000)
    assert lw.is_fresh(ts_ms)


def test_is_fresh_stale():
    ts_ms = int((time.time() - 120) * 1000)
    assert not lw.is_fresh(ts_ms)


# ---------------------------------------------------------------------------
# Real payload (captured from Linear, sanitized)
# ---------------------------------------------------------------------------

# Captured with LINEAR_WEBHOOK_CAPTURE_DIR (see README) by adding a label to an issue, then sanitized.
_REAL_PAYLOAD = Path(__file__).parent / "fixtures" / "linear_issue_update_label_added.json"


@pytest.mark.skipif(not _REAL_PAYLOAD.exists(), reason=f"capture a real Linear delivery into {_REAL_PAYLOAD.name}")
async def test_real_linear_payload_schedules_issue(client, monkeypatch):
    """A real Issue delivery (with a fresh timestamp) is accepted and schedules its data.id."""
    import app.routers.webhooks as wh_router

    fixture = json.loads(_REAL_PAYLOAD.read_text())
    payload = {**fixture["payload"], "webhookTimestamp": int(time.time() * 1000)}
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SECRET)
    scheduled = []

    async def _track(issue_id):
        scheduled.append(issue_id)

    monkeypatch.setattr(wh_router, "process_issue", _track)

    body = json.dumps(payload).encode()
    resp = await _post_signed(client, body, **{"Linear-Delivery": fixture["headers"]["Linear-Delivery"]})

    assert resp.status_code == 200
    assert scheduled == [payload["data"]["id"]]
    # Shape confirmed from the capture: previous label ids are in updatedFrom.
    assert "labelIds" in payload["updatedFrom"]
