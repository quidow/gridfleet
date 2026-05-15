"""Host router pulls capabilities and version guidance through dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient

from agent_app.host.dependencies import (
    get_capabilities_snapshot_dep,
    get_host_telemetry_dep,
    get_registered_flag,
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


async def test_health_includes_registered_flag(client: AsyncClient) -> None:
    app.dependency_overrides[get_registered_flag] = lambda: False
    try:
        resp = await client.get("/agent/health")
        assert resp.status_code == 200
        assert resp.json()["registered"] is False
    finally:
        app.dependency_overrides.pop(get_registered_flag, None)

    app.dependency_overrides[get_registered_flag] = lambda: True
    try:
        resp = await client.get("/agent/health")
        assert resp.status_code == 200
        assert resp.json()["registered"] is True
    finally:
        app.dependency_overrides.pop(get_registered_flag, None)


async def test_host_telemetry_uses_override(client: AsyncClient) -> None:
    async def _fake() -> dict[str, Any]:
        return {"cpu_percent": 7.5, "memory_used_mb": 1024, "memory_total_mb": 8192, "disk_percent": 12.0}

    app.dependency_overrides[get_host_telemetry_dep] = _fake
    try:
        resp = await client.get("/agent/host/telemetry")
        assert resp.status_code == 200
        assert resp.json() == {
            "cpu_percent": 7.5,
            "memory_used_mb": 1024,
            "memory_total_mb": 8192,
            "disk_percent": 12.0,
        }
    finally:
        app.dependency_overrides.pop(get_host_telemetry_dep, None)
