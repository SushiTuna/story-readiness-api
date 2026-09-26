"""Load the example Scrum backlog into a running server, through the stored-stories API.

Creates the stories in scripts/example_stories.json on the "Example backlog"
board (created if no board has its key prefix yet), puts each in its board
column, assesses the ones marked ``assess`` (one real Jev call each), and edits
the ones with ``edit_after_assess`` afterwards so they show as stale. Stories
whose title already exists are skipped, so the script can be re-run.

    PYTHONPATH=src uv run python scripts/seed_example_stories.py
    PYTHONPATH=src uv run python scripts/seed_example_stories.py --api http://127.0.0.1:8000 --no-assess
    PYTHONPATH=src uv run python scripts/seed_example_stories.py --board-prefix DEMO
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

DEFAULT_API = "http://127.0.0.1:8000"
DEFAULT_BOARD_PREFIX = "EX"
BOARD_NAME = "Example backlog"
EXAMPLES = Path(__file__).resolve().parent / "example_stories.json"
_FIELDS = ("title", "description", "acceptance_criteria", "definition_of_ready", "tags")


def _story_body(entry: dict) -> dict:
    return {"definition_of_ready": [], **{k: entry[k] for k in _FIELDS if k in entry}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default=DEFAULT_API, help=f"server base URL (default: {DEFAULT_API})")
    parser.add_argument("--no-assess", action="store_true", help="create and place the stories without calling Jev")
    parser.add_argument(
        "--board-prefix",
        default=DEFAULT_BOARD_PREFIX,
        help=f"key prefix of the board to fill; created as {BOARD_NAME!r} if missing (default: {DEFAULT_BOARD_PREFIX})",
    )
    args = parser.parse_args()

    stories = json.loads(EXAMPLES.read_text())["stories"]
    with httpx.Client(base_url=args.api, timeout=120.0) as client:
        try:
            boards = client.get("/api/boards").raise_for_status().json()
        except httpx.HTTPError as exc:
            print(f"Cannot reach the stored-stories API at {args.api}: {exc}", file=sys.stderr)
            return 1
        board = next((b for b in boards if b["key_prefix"] == args.board_prefix), None)
        if board is None:
            body = {"name": BOARD_NAME, "key_prefix": args.board_prefix}
            board = client.post("/api/boards", json=body).raise_for_status().json()
            print(f"board  {board['name']} ({board['key_prefix']}) created")
        stories_url = f"/api/boards/{board['id']}/stories"
        existing = {s["title"] for s in client.get(stories_url).raise_for_status().json()}

        for position, entry in enumerate(stories, start=1):
            title = entry["title"]
            if title in existing:
                print(f"skip   {title} (already exists)")
                continue

            story = client.post(stories_url, json=_story_body(entry)).raise_for_status().json()
            move = {"status": entry["status"], "position": position}
            if "blocked_reason" in entry:
                move["blocked_reason"] = entry["blocked_reason"]
            if "done_evidence" in entry:
                move["done_evidence"] = entry["done_evidence"]
            client.put(f"/api/stories/{story['id']}/move", json=move).raise_for_status()

            result = "not assessed"
            if entry.get("assess") and not args.no_assess:
                resp = client.post(f"/api/stories/{story['id']}/assess")
                if resp.status_code == 200:
                    report = resp.json()
                    result = f"{report['verdict']} {report['quality']:.0%}"
                else:
                    result = f"assessment failed (HTTP {resp.status_code}: {resp.json().get('detail')})"

            if edit := entry.get("edit_after_assess"):
                client.put(f"/api/stories/{story['id']}", json={**_story_body(entry), **edit}).raise_for_status()
                result += ", then edited"

            print(f"added  {title} [{entry['status']}] {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
