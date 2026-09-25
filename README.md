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

## API Overview

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/assess` | Assess a pasted story |
| `GET` | `/api/sources` | List configured issue trackers |
| `POST` | `/api/sources/{name}/assess` | Import and assess from Jira or Linear (stub) |
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

## Weights & Thresholds

Default check weights live in `src/app/weights.py`. The quality threshold (0.6) and unsure band (0.35–0.65) are defined in `src/app/engine.py`. These are starting points — adjust to your team's Definition of Ready.
