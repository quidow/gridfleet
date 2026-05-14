"""Appium router pulls the process manager through a FastAPI dependency."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from agent_app.appium.dependencies import get_appium_mgr
from agent_app.main import app


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
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
