# Story Refinement — Implementation Plan

## Overview

Scaffold a Python project using `uv` and implement the `openapi.yaml` (Story Readiness API v0.1.0) with FastAPI. The service accepts a Scrum user story (pasted or imported from Jira/Linear), sends it to TypeSafe's Jev model in a **single API call**, and applies in-code classification rules, weights, and thresholds to produce a structured readiness report (`ReportOut`).

**Key constraints:**
- `TYPESAFE_API_KEY` is provided via environment variable — never hardcoded.
- Jira and Linear import endpoints are stubs returning `501`.
- Story classification (user_feature / technical / bug) drives which checks are active — this logic lives in Python code, not in the Jev prompt.
- All Pydantic models must match the OpenAPI schemas exactly so FastAPI can auto-generate the spec.

---

## Sub-Tasks

---

### Sub-Task 1 — Project Scaffold with uv

**Status:** `[ ] pending`

**Intent:**  
Bootstrap a clean Python project using `uv` so all subsequent sub-tasks have a working dependency environment with FastAPI, the `typesafe-sdk` (for Jev), and python-dotenv.

**Expected Outcomes:**
- `pyproject.toml` exists with `[project]` metadata and `[tool.uv]` configuration.
- `uv.lock` is generated.
- A minimal `src/app/` package exists with an `__init__.py` and a placeholder `main.py` that starts an empty FastAPI app.
- `.env.example` is updated (or a note is added to README) documenting `TYPESAFE_API_KEY`, `JIRA_TOKEN`, `JIRA_BASE_URL`, `LINEAR_TOKEN` as required/optional env vars.
- `uv run uvicorn app.main:app --reload` starts the server without errors.

**Todo List:**
1. Run `uv init --name story-refinement --python 3.12` in the workspace root.
2. Add dependencies: `fastapi[standard]`, `typesafe-sdk`, `python-dotenv`.
3. Add dev dependencies: `pytest`, `pytest-asyncio`, `httpx` (test client).
4. Create `src/app/__init__.py` (empty).
5. Create `src/app/main.py` with a bare `FastAPI()` instance and a health-check `GET /health` route.
6. Add a `[tool.pytest.ini_options]` section and an `asyncio_mode = "auto"` setting.
7. Update `.env.example` with all required/optional keys and their descriptions.
8. Verify `uv run uvicorn app.main:app --reload` starts successfully.

**Relevant Context:**
- Workspace root: `/Users/cjlubosana/Develop/experiment/ibm-bob/refiner/story-refinement`
- No existing Python files — build from scratch.
- `.gitignore` already excludes `.env`, `__pycache__/`, `.venv`.

---

### Sub-Task 2 — Pydantic Schemas (OpenAPI models)

**Status:** `[ ] pending`

**Intent:**  
Define all Pydantic v2 models that correspond to the OpenAPI component schemas. FastAPI will derive the OpenAPI spec from these models automatically, so they must mirror the schema definitions precisely (field names, types, constraints, optionality).

**Expected Outcomes:**
- `src/app/schemas.py` contains all models: `AssessRequest`, `ImportRequest`, `StoryOut`, `StoryType`, `CheckOut`, `ReportOut`, `SourceOut`, `ErrorOut`.
- All field constraints (`maxLength`, `minLength`, `maxItems`, `ge`, `le`) are encoded as Pydantic `Field(...)` annotations.
- `StoryType.choice`, `StoryOut.source`, `SourceOut.name`, `CheckOut.kind`, `ReportOut.verdict` use `Literal` or `Enum` types matching the OpenAPI enums exactly.
- `model_config = ConfigDict(use_enum_values=True)` is set where enums are used.
- The models pass a basic round-trip test (instantiate → `.model_dump()` → re-instantiate).

**Todo List:**
1. Create `src/app/schemas.py`.
2. Define `StoryTypeChoice`, `StorySource`, `CheckKind`, `VerdictEnum` as `str` enums.
3. Define `AssessRequest` with all four fields and field-level constraints.
4. Define `ImportRequest` with `key` and `definition_of_ready`.
5. Define `StoryOut`, `StoryType`, `CheckOut`, `ReportOut`, `SourceOut`, `ErrorOut`.
6. Add `model_config` where needed.
7. Write `tests/test_schemas.py` with smoke-test instantiation of each model.

**Relevant Context:**
- OpenAPI schemas: see `openapi.yaml` `components/schemas` section.
- Key constraint: `definition_of_ready` max 50 items, each item max 300 chars, at most 12 used. The 12-item trimming and 300-char truncation is pre-processing logic (Sub-Task 4), not a Pydantic constraint.
- `CheckOut.answer` is `dict[str, int | float | str]`.
- `ReportOut.model` field conflicts with Pydantic v2's reserved `model_` namespace — use `Field(alias="model")` or rename internally and alias for serialization.

---

### Sub-Task 3 — TypeSafe Jev Client

**Status:** `[ ] pending`

**Intent:**
Build the Jev client using the official `typesafe-sdk` Python package instead of raw HTTP. This provides SDK-managed auth, retry behaviour, and typed response objects — hardening the integration compared to a hand-rolled `httpx` client.

**Expected Outcomes:**
- `src/app/jev_client.py` contains a `JevClient` class wrapping the `typesafe-sdk` client.
- The client reads `TYPESAFE_API_KEY` from environment at startup and passes it to the SDK — never hardcoded.
- The SDK client is instantiated once (module-level or via `functools.lru_cache` / FastAPI `lifespan`) and reused across requests.
- The Jev request includes: story title, description, acceptance criteria, story type classification task, and all per-check questions in a single structured call using the SDK's structured-output or prompt API.
- The response is mapped into a typed `JevResponse` dataclass capturing: `model`, `request_id`, `input_tokens`, `latency_ms`, and raw check answers keyed by check id — sourced from SDK response fields.
- SDK errors (auth failures, rate limits, upstream errors) are caught and re-raised as a custom `JevError(status_code, detail)` exception so the route layer can return `502`.
- A `MockJevClient` (or pytest fixture that monkeypatches `call_jev`) is available for tests returning a deterministic `JevResponse` — no live SDK calls in tests.

**Todo List:**
1. Run `uv add typesafe-sdk` to add the SDK dependency.
2. Create `src/app/jev_client.py`.
3. Define `JevResponse` as a Pydantic model or dataclass with fields: `model`, `request_id`, `input_tokens`, `latency_ms`, `answers: dict[str, Any]`.
4. Instantiate the `typesafe-sdk` client using `TYPESAFE_API_KEY` from env; raise `RuntimeError` at startup if the key is absent.
5. Implement `async def call_jev(story, checks_to_run) -> JevResponse` using the SDK's async interface.
6. Build the structured prompt: serialize story fields + enumerate checks as labelled questions.
7. Map SDK response fields to `JevResponse` (model name, request id, token count, latency, per-check answers).
8. Catch SDK exceptions and wrap as `JevError`.
9. Create `tests/test_jev_client.py` with a monkeypatched mock — assert correct prompt construction and `JevResponse` mapping without hitting the live API.

**Relevant Context:**
- Install with: `uv add typesafe-sdk`.
- `TYPESAFE_API_KEY` env var — the only credential needed; no base URL override required when using the SDK.
- The SDK handles retries and transport internally; do not layer `httpx` on top.
- The checks Jev must answer vary by story type — the caller (Sub-Task 4) passes the relevant check list to `call_jev`.
- `httpx` is still a dev dependency for FastAPI `TestClient` in other test files.

---

### Sub-Task 4 — Assessment Engine (Classification + Checks + Verdict)

**Status:** `[ ] pending`

**Intent:**  
Implement the in-code logic that sits between the FastAPI route and the Jev client. It: pre-processes the story (splits AC out of description, normalises DoR items), calls Jev once, classifies the story type from Jev's output, runs per-type check rules (weights, blockers, flags), computes the quality score, and determines the verdict.

**Expected Outcomes:**
- `src/app/engine.py` exports `async def assess(request: AssessRequest, source: str = "paste", key: str | None = None) -> ReportOut`.
- Pre-processing: if `acceptance_criteria` is empty, extract any "Acceptance Criteria" section from `description` into `acceptance_criteria`.
- DoR normalisation: strip blank/duplicate items, truncate each to 300 chars, cap at 12 items.
- Story type classification is driven by Jev's single-call output (not a separate call).
- Check catalogue per story type:

  | Check id          | Kind     | Story types         | Notes                             |
  |-------------------|----------|---------------------|-----------------------------------|
  | `ac_present`      | blocker  | all                 | AC field non-empty                |
  | `ac_quality`      | weighted | all                 | Jev: are ACs testable?            |
  | `has_persona`     | blocker  | user_feature only   | Jev: persona present?             |
  | `value_statement` | weighted | user_feature, bug   | Jev: value/impact stated?         |
  | `failure_handling`| weighted | user_feature, bug   | Jev: failure/edge cases covered?  |
  | `safe_rollout`    | weighted | technical only      | Jev: rollout safety described?    |
  | `title_clarity`   | weighted | all                 | Jev: is the title clear?          |
  | `scope_size`      | flag     | all                 | Jev: story seems oversized?       |
  | `dor_N`           | weighted | all (if DoR given)  | Jev: yes/no per DoR item          |

- Verdict logic (first match):
  1. `not_ready` — any blocker check fails confidently (`passed=False` and `unsure=False`).
  2. `needs_refinement` — weighted quality score < 0.6.
  3. `discuss` — any check is `unsure=True`.
  4. `ready`.
- `quality` = weighted average of all `weighted` checks' `value`.
- `src/app/weights.py` holds the default weight constants.
- `tests/test_engine.py` covers verdict boundary cases (all pass → ready, blocker fail → not_ready, quality edge → needs_refinement, unsure → discuss).

**Todo List:**
1. Create `src/app/weights.py` with default weight map keyed by check id.
2. Create `src/app/engine.py` with the `assess()` function.
3. Implement AC extraction from description (regex heading match).
4. Implement DoR normalisation.
5. Build check catalogue: a list of `CheckDef(id, label, kind, story_types)` structs.
6. After calling Jev, map Jev answers to `CheckOut` instances using the check catalogue and weights.
7. Implement quality score computation.
8. Implement verdict logic.
9. Write `tests/test_engine.py` with mocked Jev client.

**Relevant Context:**
- Weight values are unvalidated starting points (per OpenAPI description) — use sensible defaults (e.g., all weighted checks = 1.0, adjusted for DoR).
- `code` kind checks (like `ac_present`) are evaluated purely in Python without Jev.
- `blocker` kind checks go to Jev for a yes/no judgment.
- The engine must capture `latency_ms`, `model`, `request_id`, `input_tokens` from the Jev response and surface them in `ReportOut`.

---

### Sub-Task 5 — FastAPI Routes

**Status:** `[ ] pending`

**Intent:**  
Wire up the three API endpoints defined in `openapi.yaml` into FastAPI routers, connecting request/response schemas (Sub-Task 2) to the assessment engine (Sub-Task 4). Jira and Linear import stubs return `501`.

**Expected Outcomes:**
- `src/app/routers/assess.py` implements `POST /api/assess` and `POST /api/sources/{name}/assess`.
- `src/app/routers/sources.py` implements `GET /api/sources`.
- All HTTP error codes from the spec are raised correctly:
  - `413` when `len(description) + len(acceptance_criteria) > 60000` (combined size limit).
  - `502` when `JevError` is caught.
  - `409` when a source's credentials are not configured.
  - `501` for Jira/Linear (stubs).
- `src/app/main.py` registers both routers under the `/api` prefix.
- FastAPI's auto-generated `/openapi.json` matches the structure of `openapi.yaml`.
- `tests/test_routes.py` uses FastAPI `TestClient` to test the happy path for `POST /api/assess` and the `GET /api/sources` list.

**Todo List:**
1. Create `src/app/routers/` package.
2. Create `src/app/routers/assess.py` with `POST /assess` and `POST /sources/{name}/assess`.
3. Create `src/app/routers/sources.py` with `GET /sources`.
4. In `assess.py`, detect configured credentials for Jira/Linear from env vars and return `409` or `501` accordingly.
5. Register routers in `main.py` with prefix `/api`.
6. Add `tags`, `summary`, and `operation_id` on each route to match `openapi.yaml`.
7. Write `tests/test_routes.py` with TestClient tests.

**Relevant Context:**
- Size limit: the combined body size across description + acceptance_criteria is ≤ 60 000 chars each (per field). The `413` error should fire if a field exceeds its individual `maxLength`.
- FastAPI handles 422 automatically from Pydantic validation — no manual wiring needed.
- `GET /api/sources` returns a static list of `[{name: "jira", configured: <bool>}, {name: "linear", configured: <bool>}]` based on env var presence.

---

### Sub-Task 6 — Configuration, Docs, and Final Validation

**Status:** `[ ] pending`

**Intent:**  
Tie up the project: write a proper README with usage instructions, update `.env.example` with all keys, ensure `uv run pytest` passes all tests, and verify the running server's `/openapi.json` structurally matches `openapi.yaml`.

**Expected Outcomes:**
- `README.md` documents: prerequisites (`uv`, Python 3.12), setup steps, environment variables, how to run, how to test, and example `curl` for `POST /api/assess`.
- `.env.example` lists all variables with inline comments.
- `uv run pytest` exits 0.
- `GET /openapi.json` from the running server contains all paths and schemas from `openapi.yaml`.

**Todo List:**
1. Finalize `.env.example` with `TYPESAFE_API_KEY`, `JIRA_TOKEN`, `JIRA_BASE_URL`, `LINEAR_TOKEN`.
2. Rewrite `README.md` replacing the hackathon template content with project-specific docs.
3. Run `uv run pytest -v` and fix any failures.
4. Start the server and compare `/openapi.json` against `openapi.yaml` for paths and schemas.

**Relevant Context:**
- Existing `README.md` is the generic IBM Hackathon template — it should be replaced.
- `.env.example` is tracked by `.bobignore` — update it via the agent without reading sensitive data.

---

## Implementation Order

```
Sub-Task 1 (Scaffold)
    → Sub-Task 2 (Schemas)
        → Sub-Task 3 (Jev Client)
        → Sub-Task 4 (Engine)  ← depends on 2 and 3
            → Sub-Task 5 (Routes) ← depends on 2 and 4
                → Sub-Task 6 (Docs + Validation)
```
