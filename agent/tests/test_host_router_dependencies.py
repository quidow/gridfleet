"""Host router pulls capabilities and version guidance through dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient

from agent_app.host.dependencies import (
    get_capabilities_snapshot_dep,
    get_version_guidance_payload,
)
from agent_app.main import app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_health_uses_capabilities_override(client: AsyncClient) -> None:
    fake_caps: dict[str, Any] = {"missing_prerequisites": ["adb"], "answers": {"ok": True}}
    app.dependency_overrides[get_capabilities_snapshot_dep] = lambda: fake_caps
    try:
        resp = await client.get("/agent/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["missing_prerequisites"] == ["adb"]
        assert body["capabilities"] == fake_caps
    finally:
        app.dependency_overrides.pop(get_capabilities_snapshot_dep, None)


async def test_health_uses_version_guidance_override(client: AsyncClient) -> None:
    fake_guidance: dict[str, Any] = {"latest_known": "0.1.42", "current": "0.1.42", "outdated": False}
    app.dependency_overrides[get_version_guidance_payload] = lambda: fake_guidance
    try:
        resp = await client.get("/agent/health")
        assert resp.status_code == 200
        assert resp.json()["version_guidance"] == fake_guidance
    finally:
        app.dependency_overrides.pop(get_version_guidance_payload, None)
