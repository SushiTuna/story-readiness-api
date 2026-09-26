"""openapi.yaml must describe the same contract as the app's generated schema.

openapi.yaml is hand-written and richer (field descriptions, examples), so the
documents are not compared verbatim. The test covers what clients rely on:
paths, response codes and their descriptions, response headers, and the
property names and length limits of each schema.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.main import app

_SPEC = yaml.safe_load((Path(__file__).parent.parent / "openapi.yaml").read_text())
_GENERATED = app.openapi()
_UNDOCUMENTED_PATHS = {"/health"}
_METHODS = {"get", "post", "put", "patch", "delete"}


def _operations(spec: dict) -> dict[tuple[str, str], dict]:
    return {
        (path, method): op
        for path, item in spec["paths"].items()
        if path not in _UNDOCUMENTED_PATHS
        for method, op in item.items()
        if method in _METHODS
    }


def _response_contract(op: dict) -> dict[str, tuple[str, list[str]]]:
    return {
        code: (resp.get("description"), sorted(resp.get("headers", {})))
        for code, resp in op["responses"].items()
    }


def _schema_contract(schema: dict) -> dict[str, dict]:
    return {
        name: {k: prop.get(k) for k in ("maxLength", "minLength", "maxItems")}
        for name, prop in schema.get("properties", {}).items()
    }


def test_same_operations():
    assert sorted(_operations(_GENERATED)) == sorted(_operations(_SPEC))


@pytest.mark.parametrize("key", sorted(_operations(_SPEC)))
def test_same_responses(key):
    assert _response_contract(_operations(_GENERATED)[key]) == _response_contract(_operations(_SPEC)[key])


@pytest.mark.parametrize("name", sorted(_SPEC["components"]["schemas"]))
def test_same_schema_properties_and_limits(name):
    generated = _GENERATED["components"]["schemas"][name]
    assert _schema_contract(generated) == _schema_contract(_SPEC["components"]["schemas"][name])
