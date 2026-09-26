"""Shared test fixtures."""

from __future__ import annotations

import httpx
import pytest

import app.linear_client as lc


def _refuse(request: httpx.Request) -> httpx.Response:
    raise RuntimeError(f"Unmocked Linear API call in a test: {request.method} {request.url}")


@pytest.fixture(autouse=True)
def isolated_linear_client(monkeypatch):
    """Give every test a Linear client that refuses real requests.

    app.main loads .env, so a real LINEAR_TOKEN may be set during tests; without
    this, a test that forgets to mock the client could write to a real workspace.
    Tests that assign ``lc._client`` directly are restored afterwards, so a mock
    client never leaks into the next test.
    """
    monkeypatch.setattr(lc, "_client", httpx.AsyncClient(transport=httpx.MockTransport(_refuse)))
