"""OpenAPI metadata invariants for every agent operation."""

from __future__ import annotations

from typing import Any

import pytest

from agent_app.main import app

_OPS_WITHOUT_REQUIRED_4XX: set[tuple[str, str]] = {
    ("GET", "/agent/health"),
    ("GET", "/agent/host/telemetry"),
    ("GET", "/agent/tools/status"),
    ("GET", "/agent/plugins"),
    ("POST", "/agent/plugins/sync"),
    # Enumeration endpoint: always returns 200 (empty list when no pack is loaded)
    ("GET", "/agent/pack/devices"),
}


def _operations() -> list[tuple[str, str, dict[str, Any]]]:
    schema = app.openapi()
    out: list[tuple[str, str, dict[str, Any]]] = []
    for path, path_item in schema["paths"].items():
        for method, op in path_item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                out.append((method.upper(), path, op))
    return out


@pytest.mark.parametrize(("method", "path", "op"), _operations())
def test_every_operation_has_summary(method: str, path: str, op: dict[str, Any]) -> None:
    assert op.get("summary"), f"{method} {path}: missing summary"


@pytest.mark.parametrize(("method", "path", "op"), _operations())
def test_every_operation_has_tags(method: str, path: str, op: dict[str, Any]) -> None:
    assert op.get("tags"), f"{method} {path}: missing tags"


@pytest.mark.parametrize(("method", "path", "op"), _operations())
def test_every_operation_declares_2xx_schema(method: str, path: str, op: dict[str, Any]) -> None:
    responses = op.get("responses", {})
    ok = responses.get("200") or responses.get("201")
    assert ok, f"{method} {path}: no 2xx response declared"
    schema_ref = ok.get("content", {}).get("application/json", {}).get("schema")
    assert schema_ref, f"{method} {path}: 2xx response has no JSON schema"


@pytest.mark.parametrize(("method", "path", "op"), _operations())
def test_operations_with_4xx_reference_error_envelope(method: str, path: str, op: dict[str, Any]) -> None:
    if (method, path) in _OPS_WITHOUT_REQUIRED_4XX:
        pytest.skip("operation has no required 4xx surface")
    responses = op.get("responses", {})
    fourxx = {code: resp for code, resp in responses.items() if code.startswith("4")}
    assert fourxx, f"{method} {path}: no 4xx response declared"
    for code, resp in fourxx.items():
        if code == "422":
            continue
        schema = resp.get("content", {}).get("application/json", {}).get("schema", {})
        ref = schema.get("$ref", "")
        assert "ErrorEnvelope" in ref, (
            f"{method} {path} {code}: response should reference ErrorEnvelope, got {schema!r}"
        )
