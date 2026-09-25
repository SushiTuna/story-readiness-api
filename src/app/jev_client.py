"""TypeSafe Jev SDK client wrapper."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import typesafe_sdk as ts

logger = logging.getLogger(__name__)


@dataclass
class JevResponse:
    model: str
    request_id: str | None
    input_tokens: int | None
    latency_ms: int | None
    answers: dict[str, ts.NoulAnswer | ts.ChoiceAnswer | ts.ScoreAnswer]


class JevError(Exception):
    """Wraps a TypeSafe SDK error with an HTTP-style status code."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# Module-level singleton — created once on first use, reused across requests.
_client: ts.AsyncTypeSafeClient | None = None


def get_client() -> ts.AsyncTypeSafeClient:
    """Return the module-level SDK client, creating it on first call.

    Reads TYPESAFE_API_KEY from the environment automatically via the SDK.
    Raises ``ts.TypeSafeAuthenticationError`` at call time if the key is absent.
    """
    global _client
    if _client is None:
        _client = ts.AsyncTypeSafeClient()
    return _client


async def call_jev(
    state: dict,
    questions: dict[str, ts.Noul | ts.Choice | ts.Score],
    model: str | None = None,
) -> JevResponse:
    """Call the Jev model with *state* and *questions* and return a ``JevResponse``.

    Pass *model* to override the client default (e.g. ``"jev-latest"``).
    All SDK-level errors are caught and re-raised as :class:`JevError` so the
    route layer can return a ``502`` response.
    """
    t0 = time.monotonic()
    try:
        client = get_client()
        resp = await client.system_one(state=state, questions=questions, model=model)
    except ts.TypeSafeAPIError as exc:
        raise JevError(status_code=exc.status, detail=str(exc)) from exc
    except ts.TypeSafeError as exc:
        raise JevError(status_code=502, detail=str(exc)) from exc
    latency_ms = int((time.monotonic() - t0) * 1000)
    try:
        request_id: str | None = resp.request_id
    except ts.TypeSafeError:
        request_id = None
    return JevResponse(
        model=resp.model,
        request_id=request_id,
        input_tokens=resp.usage.input_tokens,
        latency_ms=latency_ms,
        answers=resp.answers,
    )
