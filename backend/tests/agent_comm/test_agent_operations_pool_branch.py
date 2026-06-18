"""Regression guards for the agent operation HTTP pool branch."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import httpx2 as httpx
import pytest

from app.agent_comm import operations as agent_operations
from app.agent_comm.http_pool import AgentHttpPool
from tests.fakes.settings import FakeSettingsReader

_VALID_HEALTH_PAYLOAD: dict[str, object] = {
    "status": "ok",
    "hostname": "agent.local",
    "os_type": "Linux",
    "version": "1.0.0",
    "registered": True,
    "version_guidance": {},
    "missing_prerequisites": [],
    "appium_processes": {},
    "capabilities": {},
}


@pytest.mark.asyncio
async def test_explicit_factory_bypasses_pool_even_when_enabled() -> None:
    """Caller-supplied http_client_factory must win over the pool."""
    seen_clients: list[httpx.AsyncClient] = []

    async def _stub_agent_request(method: str, url: str, *, client: httpx.AsyncClient, **kw: object) -> httpx.Response:
        seen_clients.append(client)
        return httpx.Response(200, json={"ok": True})

    constructed: list[httpx.AsyncClient] = []

    class _Spy(httpx.AsyncClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            constructed.append(self)

    fresh_pool = AgentHttpPool()
    settings = FakeSettingsReader({"agent.http_pool_enabled": True})
    with (
        patch("app.agent_comm.operations.agent_request", _stub_agent_request),
    ):
        response = await agent_operations._send_request(
            "GET",
            "http://10.0.0.1:5100/health",
            endpoint="/health",
            host="10.0.0.1",
            agent_port=5100,
            timeout=5,
            http_client_factory=_Spy,
            settings=settings,
            circuit_breaker=Mock(),
        )
        assert response.status_code == 200
        assert len(constructed) == 1
        assert seen_clients == constructed
        assert fresh_pool.size() == 0
    await fresh_pool.close()


@pytest.mark.asyncio
async def test_pool_used_when_enabled_and_no_explicit_factory() -> None:
    """Default factory plus setting on uses the pool path."""
    seen_clients: list[httpx.AsyncClient] = []

    async def _stub_agent_request(method: str, url: str, *, client: httpx.AsyncClient, **kw: object) -> httpx.Response:
        seen_clients.append(client)
        return httpx.Response(200, json={"ok": True})

    fresh_pool = AgentHttpPool()
    settings = FakeSettingsReader(
        {
            "agent.http_pool_enabled": True,
            "agent.http_pool_max_keepalive_connections": 10,
            "agent.http_pool_timeout_sec": 60,
        }
    )
    with (
        patch("app.agent_comm.operations.agent_request", _stub_agent_request),
    ):
        response = await agent_operations._send_request(
            "GET",
            "http://10.0.0.1:5100/health",
            endpoint="/health",
            host="10.0.0.1",
            agent_port=5100,
            timeout=5,
            settings=settings,
            pool=fresh_pool,
            circuit_breaker=Mock(),
        )
        assert response.status_code == 200
        assert fresh_pool.size() == 1
        pooled = await fresh_pool.get_client("10.0.0.1", 5100)
        assert seen_clients == [pooled]
    await fresh_pool.close()


def test_pool_enabled_returns_false_when_settings_cache_uninitialized() -> None:
    from app.agent_comm.operations import _pool_enabled

    class _RaisingSettings:
        def get(self, key: str) -> object:
            raise KeyError("not initialised")

    assert _pool_enabled(settings=_RaisingSettings()) is False

    class _RuntimeSettings:
        def get(self, key: str) -> object:
            raise RuntimeError("not initialised")

    assert _pool_enabled(settings=_RuntimeSettings()) is False


@pytest.mark.asyncio
async def test_default_factory_with_uninitialized_cache_uses_legacy_path() -> None:
    """Default factory plus settings cache error falls back to per-call client."""
    seen_clients: list[httpx.AsyncClient] = []

    async def _stub_agent_request(method: str, url: str, *, client: httpx.AsyncClient, **kw: object) -> httpx.Response:
        seen_clients.append(client)
        return httpx.Response(200, json={"ok": True})

    fresh_pool = AgentHttpPool()
    settings = FakeSettingsReader({"agent.http_pool_enabled": False})
    with (
        patch("app.agent_comm.operations.agent_request", _stub_agent_request),
    ):
        response = await agent_operations._send_request(
            "GET",
            "http://10.0.0.1:5100/health",
            endpoint="/health",
            host="10.0.0.1",
            agent_port=5100,
            timeout=5,
            settings=settings,
            circuit_breaker=Mock(),
        )
        assert response.status_code == 200
        assert fresh_pool.size() == 0
        assert len(seen_clients) == 1
        assert seen_clients[0].is_closed


@pytest.mark.asyncio
async def test_disabled_setting_uses_legacy_path_with_default_factory() -> None:
    """Disabled setting plus default factory uses the per-call client path."""
    seen_clients: list[httpx.AsyncClient] = []

    async def _stub_agent_request(method: str, url: str, *, client: httpx.AsyncClient, **kw: object) -> httpx.Response:
        seen_clients.append(client)
        return httpx.Response(200, json={"ok": True})

    fresh_pool = AgentHttpPool()
    settings = FakeSettingsReader({"agent.http_pool_enabled": False, "agent.http_pool_max_keepalive_connections": 10})
    with (
        patch("app.agent_comm.operations.agent_request", _stub_agent_request),
    ):
        response = await agent_operations._send_request(
            "GET",
            "http://10.0.0.1:5100/health",
            endpoint="/health",
            host="10.0.0.1",
            agent_port=5100,
            timeout=5,
            settings=settings,
            circuit_breaker=Mock(),
        )
        assert response.status_code == 200
        assert fresh_pool.size() == 0
        assert len(seen_clients) == 1
        assert seen_clients[0].is_closed


# ---------------------------------------------------------------------------
# Auth forwarding via pool branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_request_pooled_branch_passes_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pool branch must forward the BasicAuth carried by the pool."""
    sentinel = httpx.BasicAuth("ops", "secret")
    pool = AgentHttpPool(agent_auth=sentinel)

    monkeypatch.setattr(agent_operations, "_pool_enabled", lambda *, settings: True)

    captured: dict[str, object] = {}

    class _PooledStub:
        async def get(self, url: str, **kwargs: object) -> httpx.Response:
            captured["kwargs"] = kwargs
            return httpx.Response(200, request=httpx.Request("GET", url), json=_VALID_HEALTH_PAYLOAD)

        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            return httpx.Response(200, request=httpx.Request("POST", url), json={})

        @property
        def is_closed(self) -> bool:
            return False

    pooled = _PooledStub()

    async def _fake_get_client(host: str, agent_port: int, **_kwargs: object) -> _PooledStub:
        captured["pool_key"] = (host, agent_port)
        return pooled

    monkeypatch.setattr(pool, "get_client", _fake_get_client)

    settings = FakeSettingsReader({"agent.http_pool_enabled": True})
    payload = await agent_operations.agent_health(
        "agent.local",
        agent_port=5100,
        settings=settings,
        pool=pool,
        circuit_breaker=AsyncMock(before_request=AsyncMock(return_value=None)),
    )
    assert payload is not None
    assert payload["status"] == "ok"
    assert captured["pool_key"] == ("agent.local", 5100)
    assert captured["kwargs"].get("auth") is sentinel  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_send_request_pooled_branch_omits_auth_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pool branch must not inject an auth kwarg when the pool has no auth."""
    pool = AgentHttpPool(agent_auth=None)

    monkeypatch.setattr(agent_operations, "_pool_enabled", lambda *, settings: True)

    captured: dict[str, object] = {}

    class _PooledStub:
        async def get(self, url: str, **kwargs: object) -> httpx.Response:
            captured["kwargs"] = kwargs
            return httpx.Response(200, request=httpx.Request("GET", url), json=_VALID_HEALTH_PAYLOAD)

        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            return httpx.Response(200, request=httpx.Request("POST", url), json={})

        @property
        def is_closed(self) -> bool:
            return False

    async def _fake_get_client(host: str, agent_port: int, **_kwargs: object) -> _PooledStub:
        return _PooledStub()

    monkeypatch.setattr(pool, "get_client", _fake_get_client)

    settings = FakeSettingsReader({"agent.http_pool_enabled": True})
    payload = await agent_operations.agent_health(
        "agent.local",
        agent_port=5100,
        settings=settings,
        pool=pool,
        circuit_breaker=AsyncMock(before_request=AsyncMock(return_value=None)),
    )
    assert payload is not None
    assert "auth" not in captured["kwargs"]  # type: ignore[union-attr]
