"""Verify pack-router query validators reject path-traversal identifiers."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient

from agent_app.main import app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_health_route_rejects_traversal_pack_id(client: AsyncClient) -> None:
    bad_pack = quote("../../../etc/passwd", safe="")
    resp = await client.get(f"/agent/pack/devices/dev1/health?pack_id={bad_pack}&platform_id=android&device_type=phone")
    assert resp.status_code == 422, resp.text


async def test_health_route_rejects_traversal_platform_id(client: AsyncClient) -> None:
    resp = await client.get("/agent/pack/devices/dev1/health?pack_id=ok&platform_id=../bad&device_type=phone")
    assert resp.status_code == 422, resp.text


async def test_health_route_accepts_valid_ids(client: AsyncClient) -> None:
    resp = await client.get(
        "/agent/pack/devices/dev1/health?pack_id=appium-uiautomator2&platform_id=android&device_type=phone"
    )
    assert resp.status_code != 422
