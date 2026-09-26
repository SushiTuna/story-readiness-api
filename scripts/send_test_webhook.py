"""Send a signed Linear-style Issue webhook to a running server.

Exercises the whole webhook flow locally, without a tunnel or a real Linear
webhook: the server then fetches the issue from Linear and acts on its labels.

    PYTHONPATH=src uv run python scripts/send_test_webhook.py CJD-5
    PYTHONPATH=src uv run python scripts/send_test_webhook.py CJD-5 --url https://<tunnel>/api/webhooks/linear

The signing secret is read from LINEAR_WEBHOOK_SECRET (``.env`` is loaded).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

DEFAULT_URL = "http://127.0.0.1:8000/api/webhooks/linear"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("issue", help="Issue key (e.g. CJD-5) or id; the server re-reads it from Linear")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"webhook URL (default: {DEFAULT_URL})")
    parser.add_argument("--action", default="update", choices=["create", "update", "remove"])
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    secret = os.getenv("LINEAR_WEBHOOK_SECRET")
    if not secret:
        print("LINEAR_WEBHOOK_SECRET is not set (in the environment or .env)", file=sys.stderr)
        return 2

    payload = {
        "type": "Issue",
        "action": args.action,
        "data": {"id": args.issue},
        "webhookTimestamp": int(time.time() * 1000),
    }
    raw = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Linear-Signature": hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
        "Linear-Delivery": f"test-{uuid.uuid4()}",
        "Linear-Event": "Issue",
    }
    resp = httpx.post(args.url, content=raw, headers=headers, timeout=10)
    print(f"HTTP {resp.status_code} {resp.text}")
    return 0 if resp.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
