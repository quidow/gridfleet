from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.helpers import create_device_record

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


pytestmark = pytest.mark.db


async def test_get_returns_empty_object(client: AsyncClient, db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device_record(db_session, host_id=db_host.id, identity_value="udid-rt-1", name="dev-1")
    await db_session.commit()

    resp = await client.get(f"/api/devices/{device.id}/test_data")
    assert resp.status_code == 200
    assert resp.json() == {}


async def test_put_replaces_and_audits(client: AsyncClient, db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device_record(db_session, host_id=db_host.id, identity_value="udid-rt-2", name="dev-2")
    await db_session.commit()

    resp = await client.put(f"/api/devices/{device.id}/test_data", json={"a": 1})
    assert resp.status_code == 200
    assert resp.json() == {"a": 1}

    history = await client.get(f"/api/devices/{device.id}/test_data/history")
    rows = history.json()
    assert len(rows) == 1
    assert rows[0]["new_test_data"] == {"a": 1}


async def test_patch_deep_merges(client: AsyncClient, db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="udid-rt-3",
        name="dev-3",
        test_data={"a": {"x": 1}},
    )
    await db_session.commit()

    resp = await client.patch(f"/api/devices/{device.id}/test_data", json={"a": {"y": 2}, "b": 3})
    assert resp.status_code == 200
    assert resp.json() == {"a": {"x": 1, "y": 2}, "b": 3}


async def test_root_array_rejected_with_422(client: AsyncClient, db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device_record(db_session, host_id=db_host.id, identity_value="udid-rt-4", name="dev-4")
    await db_session.commit()

    resp = await client.put(f"/api/devices/{device.id}/test_data", json=["not", "an", "object"])
    assert resp.status_code == 422


async def test_size_cap_rejected_with_422(client: AsyncClient, db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device_record(db_session, host_id=db_host.id, identity_value="udid-rt-5", name="dev-5")
    await db_session.commit()

    big = {"k": "x" * (64 * 1024 + 100)}
    resp = await client.put(f"/api/devices/{device.id}/test_data", json=big)
    assert resp.status_code == 422


async def test_test_data_does_not_clear_verified_at(
    client: AsyncClient, db_session: AsyncSession, db_host: Host
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="udid-rt-6",
        name="dev-6",
        verified=True,
    )
    await db_session.commit()
    pre = device.verified_at

    await client.put(f"/api/devices/{device.id}/test_data", json={"v": 1})

    await db_session.refresh(device)
    assert device.verified_at == pre
