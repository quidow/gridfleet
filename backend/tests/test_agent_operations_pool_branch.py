"""Regression guards for the agent operation HTTP pool branch."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app.services import agent_operations
from app.services.agent_http_pool import AgentHttpPool


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
    with (
        patch("app.services.agent_operations.agent_http_pool", fresh_pool),
        patch(
            "app.services.agent_operations.settings_service.get",
            side_effect=lambda key: True if key == "agent.http_pool_enabled" else None,
        ),
        patch("app.services.agent_operations.agent_request", _stub_agent_request),
    ):
        response = await agent_operations._send_request(
            "GET",
            "http://10.0.0.1:5100/health",
            endpoint="/health",
            host="10.0.0.1",
            agent_port=5100,
            timeout=5,
            http_client_factory=_Spy,
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
    with (
        patch("app.services.agent_operations.agent_http_pool", fresh_pool),
        patch(
            "app.services.agent_operations.settings_service.get",
            side_effect=lambda key: True if key == "agent.http_pool_enabled" else 10 if "max_keepalive" in key else 60,
        ),
        patch("app.services.agent_operations.agent_request", _stub_agent_request),
    ):
        response = await agent_operations._send_request(
            "GET",
            "http://10.0.0.1:5100/health",
            endpoint="/health",
            host="10.0.0.1",
            agent_port=5100,
            timeout=5,
        )
        assert response.status_code == 200
        assert fresh_pool.size() == 1
        pooled = await fresh_pool.get_client("10.0.0.1", 5100)
        assert seen_clients == [pooled]
    await fresh_pool.close()


def test_pool_enabled_returns_false_when_settings_cache_uninitialized() -> None:
    from app.services.agent_operations import _pool_enabled

    with patch("app.services.agent_operations.settings_service.get", side_effect=KeyError("not initialised")):
        assert _pool_enabled() is False

    with patch("app.services.agent_operations.settings_service.get", side_effect=RuntimeError("not initialised")):
        assert _pool_enabled() is False


@pytest.mark.asyncio
async def test_default_factory_with_uninitialized_cache_uses_legacy_path() -> None:
    """Default factory plus settings cache error falls back to per-call client."""
    seen_clients: list[httpx.AsyncClient] = []

    async def _stub_agent_request(method: str, url: str, *, client: httpx.AsyncClient, **kw: object) -> httpx.Response:
        seen_clients.append(client)
        return httpx.Response(200, json={"ok": True})

    fresh_pool = AgentHttpPool()
    with (
        patch("app.services.agent_operations.agent_http_pool", fresh_pool),
        patch("app.services.agent_operations.settings_service.get", side_effect=KeyError("not initialised")),
        patch("app.services.agent_operations.agent_request", _stub_agent_request),
    ):
        response = await agent_operations._send_request(
            "GET",
            "http://10.0.0.1:5100/health",
            endpoint="/health",
            host="10.0.0.1",
            agent_port=5100,
            timeout=5,
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
    with (
        patch("app.services.agent_operations.agent_http_pool", fresh_pool),
        patch(
            "app.services.agent_operations.settings_service.get",
            side_effect=lambda key: False if key == "agent.http_pool_enabled" else 10,
        ),
        patch("app.services.agent_operations.agent_request", _stub_agent_request),
    ):
        response = await agent_operations._send_request(
            "GET",
            "http://10.0.0.1:5100/health",
            endpoint="/health",
            host="10.0.0.1",
            agent_port=5100,
            timeout=5,
        )
        assert response.status_code == 200
        assert fresh_pool.size() == 0
        assert len(seen_clients) == 1
        assert seen_clients[0].is_closed
