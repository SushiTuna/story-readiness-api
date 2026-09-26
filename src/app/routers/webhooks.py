"""POST /webhooks/linear — Linear Issue webhook receiver."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from app.linear_webhook import _is_duplicate, is_fresh, process_issue, verify_signature
from app.schemas import ErrorOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

# Headers worth keeping when capturing deliveries (the signature is left out).
_CAPTURED_HEADERS = ("Linear-Delivery", "Linear-Event", "Linear-Timestamp", "User-Agent")


def _capture(request: Request, payload: dict) -> None:
    """Save a verified delivery to LINEAR_WEBHOOK_CAPTURE_DIR, for building test fixtures.

    Development aid only; off unless the variable is set.
    """
    capture_dir = os.getenv("LINEAR_WEBHOOK_CAPTURE_DIR")
    if not capture_dir:
        return
    delivery_id = request.headers.get("Linear-Delivery") or "no-delivery-id"
    record = {
        "headers": {h: request.headers.get(h) for h in _CAPTURED_HEADERS},
        "payload": payload,
    }
    path = Path(capture_dir)
    try:
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{Path(delivery_id).name}.json").write_text(json.dumps(record, indent=2))
    except OSError as exc:
        logger.warning("Webhook: could not capture delivery %s: %s", delivery_id, exc)


def _bad_payload() -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=ErrorOut(detail="Invalid webhook payload.").model_dump(),
    )


@router.post(
    "/webhooks/linear",
    operation_id="linearWebhook",
    summary="Receive a Linear Issue webhook",
    description=(
        "Verifies the HMAC-SHA256 signature and the replay window, then schedules "
        "processing of the issue. Returns 200 immediately so Linear does not retry."
    ),
    responses={
        200: {"description": "Accepted (processing happens asynchronously)."},
        400: {"model": ErrorOut, "description": "The signed payload is not a valid webhook body."},
        401: {"model": ErrorOut, "description": "Invalid or missing signature, or stale webhook timestamp."},
        409: {"model": ErrorOut, "description": "LINEAR_WEBHOOK_SECRET is not configured on the server."},
    },
    status_code=200,
)
async def linear_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    secret = os.getenv("LINEAR_WEBHOOK_SECRET", "")
    if not secret:
        logger.warning("LINEAR_WEBHOOK_SECRET is not set — rejecting webhook")
        return JSONResponse(
            status_code=409,
            content=ErrorOut(detail="LINEAR_WEBHOOK_SECRET is not configured on the server.").model_dump(),
        )

    raw_body = await request.body()
    if not verify_signature(raw_body, request.headers.get("Linear-Signature"), secret):
        logger.warning("Webhook: invalid signature from %s", request.client)
        return JSONResponse(
            status_code=401,
            content=ErrorOut(detail="Invalid webhook signature.").model_dump(),
        )

    # The body is authentic from here on, but may still be malformed.
    try:
        payload = json.loads(raw_body)
    except ValueError:
        return _bad_payload()
    if not isinstance(payload, dict):
        return _bad_payload()

    ts_ms = payload.get("webhookTimestamp")
    if ts_ms is None:
        return JSONResponse(
            status_code=401,
            content=ErrorOut(detail="Webhook timestamp is stale or missing.").model_dump(),
        )
    if isinstance(ts_ms, bool) or not isinstance(ts_ms, (int, float)):
        return _bad_payload()
    if not is_fresh(ts_ms):
        logger.warning("Webhook: stale timestamp (%s)", ts_ms)
        return JSONResponse(
            status_code=401,
            content=ErrorOut(detail="Webhook timestamp is stale or missing.").model_dump(),
        )

    _capture(request, payload)

    # Deduplicate on delivery id.
    delivery_id = request.headers.get("Linear-Delivery", "")
    if delivery_id and _is_duplicate(delivery_id):
        logger.debug("Webhook: duplicate delivery %s — ignoring", delivery_id)
        return JSONResponse(status_code=200, content={"ok": True})

    # Only care about Issue events.
    event_type = payload.get("type")
    action = payload.get("action")
    if event_type != "Issue" or action not in ("create", "update"):
        logger.debug("Webhook: ignoring event type=%s action=%s", event_type, action)
        return JSONResponse(status_code=200, content={"ok": True})

    data = payload.get("data")
    issue_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(issue_id, str) or not issue_id:
        logger.warning("Webhook: Issue event with no data.id")
        return JSONResponse(status_code=200, content={"ok": True})

    logger.debug("Webhook: scheduling process_issue(%s) for delivery %s", issue_id, delivery_id)
    background_tasks.add_task(process_issue, issue_id)
    return JSONResponse(status_code=200, content={"ok": True})
