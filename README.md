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
| `LINEAR_TOKEN` | Optional | Linear personal API key |
| `LINEAR_WEBHOOK_SECRET` | Optional | Secret from the Linear webhook settings page |
| `LINEAR_TRIGGER_LABEL` | Optional | Label name that triggers auto-assessment (default: `ready-check`) |
| `LINEAR_WEBHOOK_CAPTURE_DIR` | Optional | Development only: save each verified webhook delivery (without its signature) to this directory, for test fixtures |

## API Overview

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/assess` | Assess a pasted story |
| `GET` | `/api/sources` | List configured issue trackers |
| `POST` | `/api/sources/linear/assess` | Import a Linear issue and assess it |
| `POST` | `/api/sources/jira/assess` | Jira import (stub — returns 501) |
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
| `needs_refinement` | Weighted quality score below 0.6 |
| `not_ready` | A blocking check failed confidently |

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
