"""Tests for resolving devices by connection_target."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.helpers import create_device_record

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.db
@pytest.mark.asyncio
async def test_returns_device_by_connection_target(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="resolve-target-1",
        connection_target="resolve-target-1",
        name="Resolve Target 1",
        operational_state="available",
    )

    resp = await client.get(f"/api/devices/by-connection-target/{device.connection_target}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(device.id)
    assert body["connection_target"] == device.connection_target


@pytest.mark.db
@pytest.mark.asyncio
async def test_returns_404_for_unknown_connection_target(client: AsyncClient) -> None:
    resp = await client.get("/api/devices/by-connection-target/does-not-exist")

    assert resp.status_code == 404
