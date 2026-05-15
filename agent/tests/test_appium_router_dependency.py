"""Appium router pulls the process manager through a FastAPI dependency."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from agent_app.appium.dependencies import get_appium_mgr
from agent_app.error_codes import AgentErrorCode
from agent_app.main import app


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_status_uses_dependency_override(client: AsyncClient) -> None:
    fake_mgr = MagicMock()
    fake_mgr.status = AsyncMock(return_value={"port": 4723, "running": False})
    app.dependency_overrides[get_appium_mgr] = lambda: fake_mgr
    try:
        resp = await client.get("/agent/appium/4723/status")
        assert resp.status_code == 200
        assert resp.json()["port"] == 4723
        fake_mgr.status.assert_awaited_once_with(4723)
    finally:
        app.dependency_overrides.pop(get_appium_mgr, None)


async def test_stop_uses_dependency_override(client: AsyncClient) -> None:
    fake_mgr = MagicMock()
    fake_mgr.stop = AsyncMock(return_value=None)
    app.dependency_overrides[get_appium_mgr] = lambda: fake_mgr
    try:
        resp = await client.post("/agent/appium/stop", json={"port": 4723})
        assert resp.status_code == 200
        assert resp.json() == {"stopped": True, "port": 4723}
        fake_mgr.stop.assert_awaited_once_with(4723)
    finally:
        app.dependency_overrides.pop(get_appium_mgr, None)


async def test_logs_uses_dependency_override(client: AsyncClient) -> None:
    fake_mgr = MagicMock()
    fake_mgr.get_logs = MagicMock(return_value=["line a", "line b"])
    app.dependency_overrides[get_appium_mgr] = lambda: fake_mgr
    try:
        resp = await client.get("/agent/appium/4723/logs?lines=10")
        assert resp.status_code == 200
        payload: dict[str, Any] = resp.json()
        assert payload["port"] == 4723
        assert payload["lines"] == ["line a", "line b"]
        assert payload["count"] == 2
        fake_mgr.get_logs.assert_called_once_with(4723, lines=10)
    finally:
        app.dependency_overrides.pop(get_appium_mgr, None)


async def test_start_unexpected_runtime_error_becomes_envelope_500(client: AsyncClient) -> None:
    class Boom:
        async def start(self, **_: object) -> None:
            raise RuntimeError("unexpected lib failure")

    app.dependency_overrides[get_appium_mgr] = lambda: Boom()
    try:
        resp = await client.post(
            "/agent/appium/start",
            json={
                "connection_target": "abc",
                "port": 4723,
                "grid_url": "http://hub:4444",
                "pack_id": "pack",
                "platform_id": "android",
            },
        )
    finally:
        app.dependency_overrides.pop(get_appium_mgr, None)

    assert resp.status_code == 500
    body = resp.json()
    assert body["detail"]["code"] == AgentErrorCode.INTERNAL_ERROR.value
    assert body["detail"]["message"] == "Internal server error"


async def test_runtime_missing_sets_retry_after(client: AsyncClient) -> None:
    from agent_app.appium.exceptions import RuntimeMissingError

    class NoRuntime:
        async def start(self, **_: object) -> None:
            raise RuntimeMissingError("no runtime")

    app.dependency_overrides[get_appium_mgr] = lambda: NoRuntime()
    try:
        resp = await client.post(
            "/agent/appium/start",
            json={
                "connection_target": "abc",
                "port": 4723,
                "grid_url": "http://hub:4444",
                "pack_id": "pack",
                "platform_id": "android",
            },
        )
    finally:
        app.dependency_overrides.pop(get_appium_mgr, None)

    assert resp.status_code == 503
    assert resp.headers.get("retry-after") == "30"


async def test_startup_timeout_sets_retry_after(client: AsyncClient) -> None:
    from agent_app.appium.exceptions import StartupTimeoutError

    class Slow:
        async def start(self, **_: object) -> None:
            raise StartupTimeoutError("timed out")

    app.dependency_overrides[get_appium_mgr] = lambda: Slow()
    try:
        resp = await client.post(
            "/agent/appium/start",
            json={
                "connection_target": "abc",
                "port": 4723,
                "grid_url": "http://hub:4444",
                "pack_id": "pack",
                "platform_id": "android",
            },
        )
    finally:
        app.dependency_overrides.pop(get_appium_mgr, None)

    assert resp.status_code == 504
    assert resp.headers.get("retry-after") == "5"
