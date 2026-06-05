"""Tests for direct backend->Appium HTTP helpers (spec §6)."""

from collections.abc import Callable

import httpx
import pytest

from app.grid import appium_direct

TARGET = "http://appium-host:4723"

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """Force ``httpx.AsyncClient()`` inside the module to use a mock transport."""
    transport = httpx.MockTransport(handler)

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(transport=transport)

    monkeypatch.setattr(appium_direct.httpx, "AsyncClient", factory)


async def test_terminate_session_404_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_transport(monkeypatch, lambda req: httpx.Response(404, json={}))
    assert await appium_direct.terminate_session(TARGET, "sess-1") is True

    _patch_transport(monkeypatch, lambda req: httpx.Response(200, json={}))
    assert await appium_direct.terminate_session(TARGET, "sess-1") is True

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    _patch_transport(monkeypatch, boom)
    assert await appium_direct.terminate_session(TARGET, "sess-1") is False


async def test_session_alive_true_on_200_false_on_invalid_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_transport(monkeypatch, lambda req: httpx.Response(200, json={"value": {}}))
    assert await appium_direct.session_alive(TARGET, "sess-1") is True

    _patch_transport(monkeypatch, lambda req: httpx.Response(404, json={}))
    assert await appium_direct.session_alive(TARGET, "sess-1") is False

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    _patch_transport(monkeypatch, boom)
    assert await appium_direct.session_alive(TARGET, "sess-1") is None

    # Non-404 error status is indeterminate, not dead.
    _patch_transport(monkeypatch, lambda req: httpx.Response(500, json={}))
    assert await appium_direct.session_alive(TARGET, "sess-1") is None


async def test_list_sessions_parses_value_array_and_none_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"value": [{"id": "a", "capabilities": {}}, {"id": "b"}, {"nope": 1}]}
    _patch_transport(monkeypatch, lambda req: httpx.Response(200, json=payload))
    assert await appium_direct.list_sessions(TARGET) == ["a", "b"]

    _patch_transport(monkeypatch, lambda req: httpx.Response(404, json={}))
    assert await appium_direct.list_sessions(TARGET) is None

    # Gated /appium/sessions (missing insecure feature) returns 500.
    _patch_transport(monkeypatch, lambda req: httpx.Response(500, json={}))
    assert await appium_direct.list_sessions(TARGET) is None

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    _patch_transport(monkeypatch, boom)
    assert await appium_direct.list_sessions(TARGET) is None


async def test_create_session_returns_session_id_or_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_transport(monkeypatch, lambda req: httpx.Response(200, json={"value": {"sessionId": "sid-1"}}))
    assert await appium_direct.create_session(TARGET, {}, timeout=5.0) == ("sid-1", None)

    _patch_transport(
        monkeypatch,
        lambda req: httpx.Response(500, json={"value": {"message": "session not created"}}),
    )
    sid, err = await appium_direct.create_session(TARGET, {}, timeout=5.0)
    assert sid is None
    assert err == "session not created"

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    _patch_transport(monkeypatch, boom)
    sid, err = await appium_direct.create_session(TARGET, {}, timeout=5.0)
    assert sid is None
    assert err is not None and err != ""

    # Non-JSON error body (e.g. HTML 502 / plain-text crash dump) must not escape
    # as a JSONDecodeError — fall back to the raw text.
    _patch_transport(
        monkeypatch,
        lambda req: httpx.Response(500, text="upstream crashed", headers={"content-type": "text/plain"}),
    )
    sid, err = await appium_direct.create_session(TARGET, {}, timeout=5.0)
    assert sid is None
    assert err == "upstream crashed"

    # A null/non-dict "value" must not raise AttributeError.
    _patch_transport(monkeypatch, lambda req: httpx.Response(500, json={"value": None}))
    sid, err = await appium_direct.create_session(TARGET, {}, timeout=5.0)
    assert sid is None
    assert err == "status 500"
