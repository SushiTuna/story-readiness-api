# Text Limit Plan

## Overview

Lower the per-field character limit for `description` and `acceptance_criteria` from 60,000 to **10,000 characters**.

The existing enforcement pattern is intentionally kept intact: the limit is checked manually in the route handler (returning HTTP 413), **not** in the Pydantic schema. This is by design — the Pydantic schema deliberately omits `max_length` so that oversized payloads reach the handler and get a 413 rather than a 422.

The change is purely a threshold update across three locations:
1. The route handler guard in `src/app/routers/assess.py`
2. The OpenAPI hint in `src/app/schemas.py` (two `json_schema_extra` annotations)
3. The existing tests in `tests/test_routes.py` that assert the 413 boundary

---

## Sub-Tasks

---

### Sub-Task 1 — Lower the guard threshold in the route handler

**Status:** `[x] done`

**Intent**
The hard limit is enforced on line 41 of `src/app/routers/assess.py`. Reducing the magic number from 60,000 to 10,000 is the only change needed to actually enforce the new limit at runtime.

**Expected Outcomes**
- Any request where `description` or `acceptance_criteria` exceeds 10,000 characters returns HTTP 413 with `{"detail": "The story is longer than the size limit."}`
- Requests at or below 10,000 characters are processed normally

**Todo List**
- [ ] In `src/app/routers/assess.py` line 41, change `60000` → `10000` (both occurrences in the single condition)

**Relevant Context**
- File: [`src/app/routers/assess.py`](src/app/routers/assess.py:41)
- The condition is `if len(body.description) > 60000 or len(body.acceptance_criteria) > 60000:`
- The error message and 413 response structure do not change

---

### Sub-Task 2 — Update the OpenAPI schema hints

**Status:** `[x] done`

**Intent**
The `json_schema_extra={"maxLength": 60000}` annotations in `AssessRequest` exist solely to advertise the documented limit to API clients via the OpenAPI spec. They must match the enforced limit so the spec stays accurate.

**Expected Outcomes**
- The generated OpenAPI spec shows `maxLength: 10000` for both `description` and `acceptance_criteria`
- No runtime behaviour changes (these annotations are documentation only)

**Todo List**
- [ ] In `src/app/schemas.py` line 54, change `"maxLength": 60000` → `"maxLength": 10000`
- [ ] In `src/app/schemas.py` line 58, change `"maxLength": 60000` → `"maxLength": 10000`

**Relevant Context**
- File: [`src/app/schemas.py`](src/app/schemas.py:52-59)
- These are `json_schema_extra` kwargs; they are not Pydantic validators and do not affect runtime enforcement

---

### Sub-Task 3 — Update tests to match the new 10,000-character boundary

**Status:** `[x] done`

**Intent**
The two existing 413 tests in `tests/test_routes.py` send strings of 60,001 characters to confirm the old limit. They must be updated to send strings of 10,001 characters so they continue testing the actual enforced boundary and don't pass for the wrong reason (a 60,001-char payload should still trigger the limit; that's not in question, but the test should document the correct threshold).

**Expected Outcomes**
- `test_oversized_description_returns_413` uses `"x" * 10001`
- `test_oversized_acceptance_criteria_returns_413` uses `"x" * 10001`
- Both tests still pass (HTTP 413 response)
- No other tests are broken

**Todo List**
- [ ] In `tests/test_routes.py` line 206, change `"x" * 60001` → `"x" * 10001`
- [ ] In `tests/test_routes.py` line 219, change `"x" * 60001` → `"x" * 10001`
- [ ] Run the full test suite to confirm all tests pass

**Relevant Context**
- File: [`tests/test_routes.py`](tests/test_routes.py:201-222)
- Helper for making a report: `_async_make_report` on line 229 (unchanged)
- Test fixture: `client` on line 79 (unchanged)
