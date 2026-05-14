"""Plugins and tools routers pull managers through FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncGenerator  # noqa: TC003 - fixture signature is runtime-inspected
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from agent_app.main import app
from agent_app.plugins.dependencies import (
    get_installed_plugins_dep,
    sync_plugins_dep,
)
from agent_app.plugins.schemas import PluginSyncRequest  # noqa: TC001 - FastAPI resolves override signatures at runtime
from agent_app.tools.dependencies import get_tool_status_dep


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_list_plugins_uses_override(client: AsyncClient) -> None:
    async def _fake() -> list[dict[str, str]]:
        return [{"name": "appium-uiautomator2-driver", "version": "1.2.3"}]

    app.dependency_overrides[get_installed_plugins_dep] = _fake
    try:
        resp = await client.get("/agent/plugins")
        assert resp.status_code == 200
        assert resp.json() == [{"name": "appium-uiautomator2-driver", "version": "1.2.3"}]
    finally:
        app.dependency_overrides.pop(get_installed_plugins_dep, None)


async def test_sync_plugins_uses_override(client: AsyncClient) -> None:
    captured: list[PluginSyncRequest] = []

    async def _fake(req: PluginSyncRequest) -> dict[str, Any]:
        captured.append(req)
        return {
            "installed": ["appium-uiautomator2-driver"],
            "updated": [],
            "removed": [],
            "errors": {},
        }

    app.dependency_overrides[sync_plugins_dep] = _fake
    try:
        resp = await client.post(
            "/agent/plugins/sync",
            json={
                "plugins": [
                    {"name": "appium-uiautomator2-driver", "version": "1.2.3", "source": "npm:appium-uiautomator2"}
                ]
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["installed"] == ["appium-uiautomator2-driver"]
        assert len(captured) == 1
        assert captured[0].plugins[0].name == "appium-uiautomator2-driver"
    finally:
        app.dependency_overrides.pop(sync_plugins_dep, None)


async def test_tools_status_uses_override(client: AsyncClient) -> None:
    async def _fake() -> dict[str, Any]:
        return {"adb": "33.0.3", "node": "v20.0.0"}

    app.dependency_overrides[get_tool_status_dep] = _fake
    try:
        resp = await client.get("/agent/tools/status")
        assert resp.status_code == 200
        assert resp.json() == {"adb": "33.0.3", "node": "v20.0.0"}
    finally:
        app.dependency_overrides.pop(get_tool_status_dep, None)
