"""Unit tests for the shared agent response decode helpers."""

import httpx2 as httpx
import pytest
from pydantic import BaseModel

from app.agent_comm.operations import (
    _decode_model_payload,
    decode_or_none_on_404,
    decode_or_none_unless_200,
    decode_or_raise,
)
from app.core.errors import AgentResponseError, AgentUnreachableError


class _Payload(BaseModel):
    ok: bool


def _response(status_code: int, *, json_body: object = None, text: str | None = None) -> httpx.Response:
    request = httpx.Request("GET", "http://agent.test/agent/health")
    if text is not None:
        return httpx.Response(status_code, text=text, request=request)
    return httpx.Response(status_code, json=json_body, request=request)


def test_strict_returns_valid_payload() -> None:
    raw = _decode_model_payload(
        _response(200, json_body={"ok": True}), host="h1", action="health check", model=_Payload
    )
    assert raw == {"ok": True}


def test_strict_raises_response_error_on_http_failure() -> None:
    with pytest.raises(AgentResponseError):
        _decode_model_payload(_response(500, json_body={}), host="h1", action="health check", model=_Payload)


def test_strict_raises_unreachable_on_invalid_json() -> None:
    with pytest.raises(AgentUnreachableError, match="invalid JSON payload"):
        _decode_model_payload(_response(200, text="not json"), host="h1", action="health check", model=_Payload)


def test_strict_raises_unreachable_on_model_mismatch() -> None:
    with pytest.raises(AgentUnreachableError, match="invalid payload"):
        _decode_model_payload(
            _response(200, json_body={"ok": "not-a-bool-at-all"}), host="h1", action="health check", model=_Payload
        )


def test_decode_or_raise_returns_valid_payload() -> None:
    raw = decode_or_raise(_response(200, json_body={"ok": True}), host="h", action="a", model=_Payload)
    assert raw == {"ok": True}


def test_decode_or_raise_returns_none_on_invalid_json_and_model_mismatch() -> None:
    assert decode_or_raise(_response(200, text="not json"), host="h", action="a", model=_Payload) is None
    assert (
        decode_or_raise(_response(200, json_body={"ok": "not-a-bool-at-all"}), host="h", action="a", model=_Payload)
        is None
    )


def test_decode_or_raise_raises_on_http_failure() -> None:
    with pytest.raises(AgentResponseError):
        decode_or_raise(_response(500, json_body={}), host="h", action="a", model=_Payload)


def test_decode_or_none_unless_200_returns_none_on_other_status() -> None:
    assert decode_or_none_unless_200(_response(503, json_body={}), host="h", action="a", model=_Payload) is None


def test_decode_or_none_on_404_returns_none_on_404() -> None:
    assert decode_or_none_on_404(_response(404, json_body={}), host="h", action="a", model=_Payload) is None


def test_decode_or_none_on_404_still_raises_on_other_http_failure() -> None:
    with pytest.raises(AgentResponseError):
        decode_or_none_on_404(_response(500, json_body={}), host="h", action="a", model=_Payload)
