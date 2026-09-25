"""Tests for src/app/jev_client.py — no live network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import typesafe_sdk as ts

from app.jev_client import JevError, JevResponse, call_jev


# ---------------------------------------------------------------------------
# Helpers — build fake SDK response objects
# ---------------------------------------------------------------------------

def _make_noul_answer(value: float = 0.9) -> ts.NoulAnswer:
    return ts.NoulAnswer(type="noul", noul=value)


def _make_choice_answer(choice: str = "user_feature") -> ts.ChoiceAnswer:
    return ts.ChoiceAnswer(
        type="choice",
        choice=choice,
        confidence=0.95,
        probabilities={"user_feature": 0.95, "technical": 0.03, "bug": 0.02},
    )


def _make_score_answer(score: float = 2.5) -> ts.ScoreAnswer:
    return ts.ScoreAnswer(
        type="score",
        score=score,
        confidence=0.8,
        legend={0: "No ACs", 1: "Partial", 2: "Clear and testable"},
        probabilities={0: 0.05, 1: 0.15, 2: 0.80},
    )


def _make_sdk_response(
    model: str = "jev-1.13.0",
    input_tokens: int | None = 120,
    answers: dict | None = None,
) -> ts.SystemOneResponse:
    if answers is None:
        answers = {
            "has_persona": _make_noul_answer(0.85),
            "story_type": _make_choice_answer("user_feature"),
            "ac_quality": _make_score_answer(2.0),
        }
    usage = ts.Usage(input_tokens=input_tokens, output_tokens=None)
    return ts.SystemOneResponse(model=model, usage=usage, answers=answers)


def _make_mock_client(sdk_response: ts.SystemOneResponse) -> MagicMock:
    """Return a mock AsyncTypeSafeClient whose system_one returns *sdk_response*."""
    mock_client = MagicMock(spec=ts.AsyncTypeSafeClient)
    mock_client.system_one = AsyncMock(return_value=sdk_response)
    return mock_client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_call_jev_returns_jev_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """call_jev maps SDK response fields to JevResponse correctly."""
    sdk_resp = _make_sdk_response()
    monkeypatch.setattr("app.jev_client.get_client", lambda: _make_mock_client(sdk_resp))

    result = await call_jev(
        state={"title": "My story"},
        questions={
            "has_persona": ts.Noul(instructions="Is there a persona?"),
            "story_type": ts.Choice(criteria={"user_feature": "...", "technical": "...", "bug": "..."}),
            "ac_quality": ts.Score(criteria=["No ACs", "Partial", "Clear and testable"]),
        },
    )

    assert isinstance(result, JevResponse)
    assert result.model == "jev-1.13.0"
    assert result.input_tokens == 120
    assert result.answers == sdk_resp.answers


async def test_request_id_extracted_from_sdk_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """request_id is read from resp.request_id on the SDK response object."""
    sdk_resp = _make_sdk_response()
    # Simulate the SDK attaching a request_id via its cached_property mechanism.
    sdk_resp.__dict__["_request_id"] = "req_abc123"
    monkeypatch.setattr("app.jev_client.get_client", lambda: _make_mock_client(sdk_resp))

    result = await call_jev(state={"title": "T"}, questions={})

    assert result.request_id == "req_abc123"


async def test_request_id_is_none_when_sdk_header_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """request_id is None when the SDK response carries no x-typesafe-request-id header."""
    sdk_resp = _make_sdk_response()
    # No "_request_id" injected → cached_property raises TypeSafeError → we return None.
    monkeypatch.setattr("app.jev_client.get_client", lambda: _make_mock_client(sdk_resp))

    result = await call_jev(state={"title": "T"}, questions={})

    assert result.request_id is None


async def test_latency_ms_is_non_negative_int(monkeypatch: pytest.MonkeyPatch) -> None:
    """latency_ms is a non-negative integer."""
    sdk_resp = _make_sdk_response()
    monkeypatch.setattr("app.jev_client.get_client", lambda: _make_mock_client(sdk_resp))

    result = await call_jev(state={"title": "T"}, questions={})

    assert isinstance(result.latency_ms, int)
    assert result.latency_ms >= 0


async def test_call_jev_raises_jev_error_on_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """TypeSafeAPIError from the SDK is re-raised as JevError with the HTTP status."""
    import httpx

    # Build a minimal fake response/request pair that TypeSafeAPIError needs.
    fake_request = httpx.Request("POST", "https://api.typesafe.ai/v1/system_one")
    fake_response = httpx.Response(
        status_code=429,
        request=fake_request,
        headers={"content-type": "application/json"},
        content=b'{"error": "rate limit exceeded"}',
    )
    api_error = ts.TypeSafeRateLimitError(
        status=429,
        body={"error": "rate limit exceeded"},
        headers=fake_response.headers,
    )

    mock_client = MagicMock(spec=ts.AsyncTypeSafeClient)
    mock_client.system_one = AsyncMock(side_effect=api_error)
    monkeypatch.setattr("app.jev_client.get_client", lambda: mock_client)

    with pytest.raises(JevError) as exc_info:
        await call_jev(state={"title": "T"}, questions={})

    err = exc_info.value
    assert err.status_code == 429
    assert isinstance(err.detail, str)


async def test_call_jev_raises_jev_error_on_base_typesafe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-API TypeSafeError (e.g. connection error) is re-raised as JevError(502)."""
    conn_error = ts.TypeSafeAPIConnectionError("Connection refused")

    mock_client = MagicMock(spec=ts.AsyncTypeSafeClient)
    mock_client.system_one = AsyncMock(side_effect=conn_error)
    monkeypatch.setattr("app.jev_client.get_client", lambda: mock_client)

    with pytest.raises(JevError) as exc_info:
        await call_jev(state={"title": "T"}, questions={})

    assert exc_info.value.status_code == 502


async def test_call_jev_passes_state_and_questions_to_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The state dict and questions are forwarded verbatim to client.system_one."""
    sdk_resp = _make_sdk_response()
    mock_client = _make_mock_client(sdk_resp)
    monkeypatch.setattr("app.jev_client.get_client", lambda: mock_client)

    state = {"title": "Add login", "description": "Users need to log in"}
    questions = {"has_persona": ts.Noul(instructions="Is there a persona?")}

    await call_jev(state=state, questions=questions)

    mock_client.system_one.assert_awaited_once_with(state=state, questions=questions, model=None)


async def test_input_tokens_none_when_sdk_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """input_tokens is None when the SDK Usage carries None."""
    sdk_resp = _make_sdk_response(input_tokens=None)
    monkeypatch.setattr("app.jev_client.get_client", lambda: _make_mock_client(sdk_resp))

    result = await call_jev(state={}, questions={})

    assert result.input_tokens is None


async def test_get_client_error_inside_try_gives_jev_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug fix: get_client() is inside the try block so auth errors produce JevError, not 500."""
    def bad_get_client():
        raise ts.TypeSafeAuthenticationError(
            status=401,
            body={"error": "No API key"},
            headers={},
        )

    monkeypatch.setattr("app.jev_client.get_client", bad_get_client)

    with pytest.raises(JevError) as exc_info:
        await call_jev(state={}, questions={})

    assert exc_info.value.status_code == 401
