"""Tests for direct backend->Appium HTTP helpers (spec §6)."""

from typing import TYPE_CHECKING

import httpx2 as httpx

from app.grid import appium_direct

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

TARGET = "http://appium-host:4723"


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """Force ``appium_direct._get_client()`` to return a mock-transport client."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(appium_direct, "_get_client", lambda: client)


async def test_terminate_session_404_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_transport(monkeypatch, lambda req: httpx.Response(404, json={}))
    assert await appium_direct.terminate_session(TARGET, "sess-1") is True

    _patch_transport(monkeypatch, lambda req: httpx.Response(200, json={}))
    assert await appium_direct.terminate_session(TARGET, "sess-1") is True

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    _patch_transport(monkeypatch, boom)
    assert await appium_direct.terminate_session(TARGET, "sess-1") is False


async def test_terminate_session_percent_encodes_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator kill endpoint made terminate reachable with request-supplied
    ids (CodeQL py/partial-ssrf): a crafted id must not alter the URL path."""
    seen: list[str] = []

    def record(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.raw_path.decode())
        return httpx.Response(200, json={})

    _patch_transport(monkeypatch, record)
    assert await appium_direct.terminate_session(TARGET, "../grid/evil?x=1#f") is True
    assert seen == ["/session/..%2Fgrid%2Fevil%3Fx%3D1%23f"]


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
    assert await appium_direct.create_session(TARGET, {}, timeout=5.0) == ("sid-1", None, False)

    _patch_transport(
        monkeypatch,
        lambda req: httpx.Response(500, json={"value": {"message": "session not created"}}),
    )
    sid, err, transport_error = await appium_direct.create_session(TARGET, {}, timeout=5.0)
    assert sid is None
    assert err == "session not created"
    assert transport_error is False

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    _patch_transport(monkeypatch, boom)
    sid, err, transport_error = await appium_direct.create_session(TARGET, {}, timeout=5.0)
    assert sid is None
    assert err is not None and err != ""
    assert transport_error is True

    # Non-JSON error body (e.g. HTML 502 / plain-text crash dump) must not escape
    # as a JSONDecodeError — fall back to the raw text. An HTTP response, not transport.
    _patch_transport(
        monkeypatch,
        lambda req: httpx.Response(500, text="upstream crashed", headers={"content-type": "text/plain"}),
    )
    sid, err, transport_error = await appium_direct.create_session(TARGET, {}, timeout=5.0)
    assert sid is None
    assert err == "upstream crashed"
    assert transport_error is False

    # A null/non-dict "value" must not raise AttributeError.
    _patch_transport(monkeypatch, lambda req: httpx.Response(500, json={"value": None}))
    sid, err, transport_error = await appium_direct.create_session(TARGET, {}, timeout=5.0)
    assert sid is None
    assert err == "status 500"
    assert transport_error is False


async def test_terminate_session_transport_error_increments_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import metrics_recorders
    from app.grid import appium_direct

    class _FailingClient:
        is_closed = False

        async def delete(self, *args: object, **kwargs: object) -> object:
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(appium_direct, "_get_client", _FailingClient)
    before = metrics_recorders.APPIUM_TERMINATE_FAILED_TOTAL._value.get()

    ok = await appium_direct.terminate_session("http://10.0.0.1:4723", "sess-transport-1")

    assert ok is False
    assert metrics_recorders.APPIUM_TERMINATE_FAILED_TOTAL._value.get() == before + 1


async def test_get_client_pools_and_aclose_resets() -> None:
    """Repeated calls reuse one pooled client; aclose() closes and clears it."""
    await appium_direct.aclose()  # start clean
    first = appium_direct._get_client()
    second = appium_direct._get_client()
    assert first is second
    assert not first.is_closed

    await appium_direct.aclose()
    assert first.is_closed
    assert appium_direct._client is None

    # A new client is minted on demand after reset.
    third = appium_direct._get_client()
    assert third is not first
    await appium_direct.aclose()

    # aclose() on an already-closed/absent client is a no-op.
    await appium_direct.aclose()
