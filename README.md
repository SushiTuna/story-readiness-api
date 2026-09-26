# Story Readiness API

Assess whether a Scrum user story is ready for sprint planning. Powered by [TypeSafe Jev](https://typesafe.ai).

## Prerequisites

- [uv](https://docs.astral.sh/uv/) ≥ 0.4
- Python 3.12 (managed automatically by uv)
- A TypeSafe API key (set as `TYPESAFE_API_KEY`)

## Setup

```bash
# 1. Clone and enter the project
git clone <repo-url> && cd story-refinement

# 2. Copy environment template
cp .env.example .env
# Edit .env and set TYPESAFE_API_KEY

# 3. Install dependencies
uv sync
```

## Running

```bash
PYTHONPATH=src uv run uvicorn app.main:app --reload
```

API docs available at http://localhost:8000/docs

## Testing

```bash
PYTHONPATH=src uv run pytest -v
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `TYPESAFE_API_KEY` | ✅ | TypeSafe API key for Jev model inference |
| `JIRA_TOKEN` | Optional | Jira personal access token |
| `JIRA_BASE_URL` | Optional | Jira instance base URL (e.g. `https://org.atlassian.net`) |
| `LINEAR_ENABLED` | Optional | `true` turns the Linear integration on (default: off). When off, the Linear endpoints and the webhook return `409` and nothing is read from or written to Linear, even with a token set |
| `LINEAR_TOKEN` | Optional | Linear personal API key |
| `LINEAR_WEBHOOK_SECRET` | Optional | Secret from the Linear webhook settings page |
| `LINEAR_TRIGGER_LABEL` | Optional | Label name that triggers auto-assessment (default: `ready-check`) |
| `LINEAR_WEBHOOK_CAPTURE_DIR` | Optional | Development only: save each verified webhook delivery (without its signature) to this directory, for test fixtures |
| `STORIES_DB_PATH` | Optional | SQLite file for stored stories and their assessment history (default: `data/stories.db`, git-ignored) |

## API Overview

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/assess` | Assess a pasted story |
| `GET` | `/api/sources` | List configured issue trackers |
| `POST` | `/api/sources/linear/assess` | Import a Linear issue and assess it |
| `POST` | `/api/sources/jira/assess` | Jira import (stub — returns 501) |
| `GET` | `/api/sources/linear/teams` | List the Linear teams the token can see |
| `GET` | `/api/sources/linear/issues?team=<id>` | List a team's open issues (read-only, up to 100) |
| `GET` `POST` | `/api/boards` | List boards (with story counts) or create one |
| `PUT` `DELETE` | `/api/boards/{id}` | Rename a board or edit its description, or delete it with all its stories |
| `GET` `POST` | `/api/boards/{id}/stories` | List a board's stored stories, or store a pasted story on it (at most 100 per board) |
| `GET` `PUT` `DELETE` | `/api/stories/{id}` | Read (with latest report and history), edit or delete a stored story |
| `PUT` | `/api/stories/{id}/move` | Move a stored story to a workflow column and position (board drag and drop) |
| `POST` | `/api/stories/{id}/assess` | Assess a stored story and save the report to its history |
| `POST` | `/api/webhooks/linear` | Inbound Linear Issue webhook (label-triggered) |
| `GET` | `/health` | Health check |

### Example: assess a story

```bash
curl -X POST http://localhost:8000/api/assess \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Reject orders with quantity above 999",
    "description": "As an API client developer, I want POST /api/orders to reject line items with quantity greater than 999 so that bulk orders go through the wholesale flow.",
    "acceptance_criteria": "- Given quantity 1000, then the response is 400 QUANTITY_TOO_LARGE\n- Given quantity 999, then the order is accepted (201)",
    "definition_of_ready": ["Security or privacy impact is described"]
  }'
```

### Verdict values

| Verdict | Meaning |
|---|---|
| `ready` | Story passes all checks |
| `discuss` | Some answers are uncertain — discuss in refinement |
| `needs_refinement` | Weighted quality score below 0.6, or an AI-agent readiness check failed confidently |
| `not_ready` | A blocking check failed confidently |

### AI-agent readiness checks

Four weighted checks estimate whether an AI coding agent (Claude Code, GitHub Copilot coding agent, Codex, IBM Bob, …) could implement the story without asking anyone. They run for every story type.

| Check | Passes when the story… |
|---|---|
| `agent_no_open_decisions` | leaves no TBDs, open questions or alternatives to choose between |
| `agent_verifiable` | says how to check the work automatically: tests, example inputs and outputs, a command |
| `agent_code_context` | names files, modules, endpoints or a pattern to follow (for a defect: the error or repro steps) |
| `agent_self_contained` | needs no human-only step: production access, credentials, approvals, missing designs |

The first three follow GitHub's [best practices for Copilot coding agent tasks](https://docs.github.com/en/copilot/how-tos/agents/copilot-coding-agent/best-practices-for-using-copilot-to-work-on-tasks) and Claude Code's [best practices](https://code.claude.com/docs/en/best-practices). The fourth is our own heuristic.

Because coding agents now do most of the implementation, these checks gate readiness:

- Each has weight 1.0, the same as the core checks, so together they are about half of the quality score (4.0 of 8.0 for a user feature, 4.0 of 7.0 for a technical story or bug, before DoR items).
- A confident failure of any one of them makes the verdict `needs_refinement`, even when the score is high: an agent can't settle open decisions or do human-only steps. A failed blocker still gives `not_ready`.
- An unsure answer gives `discuss`, like any other check.

### Stored stories

Pasted stories can be kept in a SQLite database (`STORIES_DB_PATH`, created on startup) together with every assessment of them. The [story-board](../story-board) frontend uses this.

- Stories live on **boards**, each scoped to one goal (e.g. "Flowershop Website" or "Platform Maintenance"). A board holds at most **100 stories**, counting every column including `done`; creating one more returns `409` until a story is deleted. `GET /api/boards` returns each board's `story_count` and `story_limit`.
- Each board has a `key_prefix` (2–6 uppercase letters or digits, starting with a letter, unique among boards) that is fixed once the board is created. Renaming a board (`PUT /api/boards/{id}`) changes only its name and description. Deleting a board deletes its stories and their assessments.
- Each story has a short `key` (`FLW-1`, `FLW-2`, …) besides its UUID `id`: the board's prefix and a number handed out in creation order. Numbers are never reused, even after a delete, and not even by a later board with the same prefix. The API still addresses stories by `id`.
- Stories stored before boards existed are moved on startup to a board named "Stories" with the prefix `ST`, so their keys don't change.
- `GET /api/boards/{id}/stories` returns each story with its latest verdict and quality, the quality before that (`previous_quality`), and `stale: true` when the story was edited after its latest assessment.
- `POST /api/stories/{id}/assess` costs one Jev call and adds a row to the history. Editing a story keeps its history.
- Each story also has a workflow `status` (`backlog`, `refinement`, `ready_for_sprint`, `in_sprint`, `done`, `blocked`) and a `position` within that column, set with `PUT /api/stories/{id}/move`. Moving is not an edit: it does not mark the story stale.
- Moving to `blocked` requires a `blocked_reason` (up to 500 characters), which is returned with the story. Moving to any other column clears it; sending a reason with another status is a `422`.
- Like the rest of `/api/*`, these endpoints have no authentication. Anyone who can reach the server can read and delete stored stories.

#### Example backlog

`scripts/example_stories.json` holds a fictional backlog of a Scrum team building a B2B spare-parts portal with a Java / Spring Boot backend and a React frontend: 14 stories across all columns and verdicts (ready, discuss, needs refinement, not ready, not assessed, one edited since its assessment, and one blocked). Load it into a running server; it goes on an "Example backlog" board (prefix `EX`, created if missing; pick another with `--board-prefix`), costs one Jev call per assessed story (12) and skips titles already on that board:

```bash
PYTHONPATH=src uv run python scripts/seed_example_stories.py            # or --no-assess
```

### Example: import and assess a Linear issue

```bash
# Basic import
curl -X POST http://localhost:8000/api/sources/linear/assess \
  -H "Content-Type: application/json" \
  -d '{"key": "ENG-123", "definition_of_ready": []}'

# With verdict posted back to the issue as a comment
curl -X POST http://localhost:8000/api/sources/linear/assess \
  -H "Content-Type: application/json" \
  -d '{"key": "ENG-123", "post_comment": true}'
```

When `post_comment` is true the response includes an `X-Linear-Comment: posted|failed` header. A comment failure does **not** affect the HTTP status — the assessment is still returned.

## Linear Webhook Setup

The webhook integration allows adding a label (default `ready-check`) to a Linear issue to trigger an assessment automatically. The result is posted back as a comment (updated on re-runs) and a `readiness:<verdict>` label.

Comments and labels are written with `LINEAR_TOKEN`, so in Linear they appear as that token's owner.

The integration is off by default. Set `LINEAR_ENABLED=true` in `.env` (and restart the server) before following these steps; while it is off, the webhook answers `409` to every delivery.

### 1. Run the server and expose it

```bash
uv run uvicorn app.main:app --reload
# In a second terminal, expose with any tunnel tool, e.g. cloudflared:
cloudflared tunnel --url http://localhost:8000
```

Copy the tunnel URL (e.g. `https://abc.trycloudflare.com`).

A quick tunnel gets a new URL on every restart, and Linear disables a webhook whose URL keeps failing. For anything longer than a test, use a named tunnel or a deployment with a stable URL. The quick tunnel also exposes `/api/assess`, which has no authentication.

### 2. Create the webhook in Linear

1. Go to **Settings → API → Webhooks** (workspace admin required).
2. Click **New webhook**.
3. Set the URL to `<tunnel-url>/api/webhooks/linear`.
4. Under **Resource types**, tick **Issues**.
5. Copy the **Signing secret** and add it to your `.env`:

```
LINEAR_WEBHOOK_SECRET=<secret from Linear>
```

### 3. Create the trigger label

In your Linear team, create a label named `ready-check` (or whatever you set in `LINEAR_TRIGGER_LABEL`).

### 4. Label flow

| Step | What happens |
|---|---|
| Add `ready-check` to an issue | Webhook fires → assessment runs in the background |
| Assessment completes | The readiness comment is **updated** (or created the first time); on re-runs it shows what changed, e.g. `Quality score: 94% (was 14%)` and resolved blockers |
| Labels | `readiness:<verdict>` is added; `ready-check` and any old `readiness:*` label are removed. Other labels are untouched |
| Re-assess | Add `ready-check` again — the same comment is updated |
| Title or description edited later | The verdict label is replaced by `readiness:stale` (no Jev call). Add `ready-check` to re-assess |
| No trigger or verdict label | The change is ignored |

The `readiness:*` labels live in a workspace label group named **Readiness**, created on first use. Linear allows only one label from a group on an issue, so an issue never shows two verdicts. Labels such as `Bug` on the issue are passed to the assessment as hints for the story type.

### Error handling

| Failure | Result in Linear |
|---|---|
| Jev call fails, or the description is over 10,000 characters | The comment says **Not assessed** with the reason, `readiness:error` is set and `ready-check` is removed. Re-add `ready-check` to retry |
| Writing the comment or labels to Linear fails | Logged on the server; `ready-check` stays, so the next change to the issue retries |
| Linear rejects `LINEAR_TOKEN` | Logged on the server; the pull endpoint returns `409` |

### Test without Linear sending the event

`scripts/send_test_webhook.py` sends a correctly signed Issue event to a running server, which then processes the real issue in Linear:

```bash
PYTHONPATH=src uv run python scripts/send_test_webhook.py CJD-5
```

### Verify the signature manually

```bash
# Send a tampered request — should get 401:
curl -s -o /dev/null -w "%{http_code}" -X POST <tunnel-url>/api/webhooks/linear \
  -H "Content-Type: application/json" \
  -H "Linear-Signature: invalidsig" \
  -d '{"type":"Issue","action":"update","data":{"id":"fake"},"webhookTimestamp":0}'
```

## Weights & Thresholds

Default check weights live in `src/app/weights.py`. The quality threshold (0.6) and unsure band (0.35–0.65) are defined in `src/app/engine.py`. These are starting points — adjust to your team's Definition of Ready.
