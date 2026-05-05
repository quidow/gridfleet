from typing import Any

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import create_device_record, create_host

HOST_PAYLOAD = {
    "hostname": "bulk-host",
    "ip": "10.0.0.30",
    "os_type": "linux",
    "agent_port": 5100,
}


@pytest_asyncio.fixture
async def default_host_id(client: AsyncClient) -> str:
    host = await create_host(client, **HOST_PAYLOAD)
    return str(host["id"])


async def _create_device(db_session: AsyncSession, identity_value: str, name: str, host_id: str) -> dict[str, Any]:
    device = await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=identity_value,
        connection_target=identity_value,
        name=name,
        os_version="14",
    )
    return {"id": str(device.id)}


async def _create_devices(db_session: AsyncSession, host_id: str, count: int = 3) -> list[str]:
    ids = []
    for i in range(count):
        device = await _create_device(db_session, f"bulk-{i}", f"Bulk Device {i}", host_id)
        ids.append(device["id"])
    return ids


async def test_bulk_set_status_route_removed(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    ids = await _create_devices(db_session, default_host_id)
    resp = await client.post("/api/devices/bulk/set-status", json={"device_ids": ids, "status": "available"})
    assert resp.status_code == 404


async def test_bulk_set_auto_manage(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    ids = await _create_devices(db_session, default_host_id, 2)
    resp = await client.post(
        "/api/devices/bulk/set-auto-manage",
        json={"device_ids": ids, "auto_manage": False},
    )
    assert resp.status_code == 200
    assert resp.json()["succeeded"] == 2

    # Verify
    for device_id in ids:
        r = await client.get(f"/api/devices/{device_id}")
        assert r.json()["auto_manage"] is False


async def test_bulk_update_tags_merge(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    ids = await _create_devices(db_session, default_host_id, 2)
    # Set initial tags on first device
    await client.patch(f"/api/devices/{ids[0]}", json={"tags": {"env": "lab"}})

    resp = await client.post(
        "/api/devices/bulk/update-tags",
        json={"device_ids": ids, "tags": {"team": "qa"}, "merge": True},
    )
    assert resp.status_code == 200
    assert resp.json()["succeeded"] == 2

    # First device should have both tags
    r = await client.get(f"/api/devices/{ids[0]}")
    tags = r.json()["tags"]
    assert tags["env"] == "lab"
    assert tags["team"] == "qa"


async def test_bulk_update_tags_replace(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    ids = await _create_devices(db_session, default_host_id, 1)
    await client.patch(f"/api/devices/{ids[0]}", json={"tags": {"env": "lab"}})

    resp = await client.post(
        "/api/devices/bulk/update-tags",
        json={"device_ids": ids, "tags": {"team": "qa"}, "merge": False},
    )
    assert resp.status_code == 200

    r = await client.get(f"/api/devices/{ids[0]}")
    tags = r.json()["tags"]
    assert "env" not in tags
    assert tags["team"] == "qa"


async def test_bulk_delete(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    ids = await _create_devices(db_session, default_host_id, 2)
    resp = await client.post("/api/devices/bulk/delete", json={"device_ids": ids})
    assert resp.status_code == 200
    assert resp.json()["succeeded"] == 2

    # Verify deleted
    for device_id in ids:
        r = await client.get(f"/api/devices/{device_id}")
        assert r.status_code == 404


async def test_bulk_enter_maintenance(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    ids = await _create_devices(db_session, default_host_id, 2)
    resp = await client.post(
        "/api/devices/bulk/enter-maintenance",
        json={"device_ids": ids, "drain": False},
    )
    assert resp.status_code == 200
    assert resp.json()["succeeded"] == 2

    for device_id in ids:
        r = await client.get(f"/api/devices/{device_id}")
        assert r.json()["hold"] == "maintenance"


async def test_bulk_exit_maintenance(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    ids = await _create_devices(db_session, default_host_id, 2)
    # Enter maintenance first
    await client.post(
        "/api/devices/bulk/enter-maintenance",
        json={"device_ids": ids, "drain": False},
    )

    resp = await client.post(
        "/api/devices/bulk/exit-maintenance",
        json={"device_ids": ids},
    )
    assert resp.status_code == 200
    assert resp.json()["succeeded"] == 2

    for device_id in ids:
        r = await client.get(f"/api/devices/{device_id}")
        assert r.json()["operational_state"] == "offline"


async def test_bulk_exit_maintenance_not_in_maintenance(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    ids = await _create_devices(db_session, default_host_id, 1)
    resp = await client.post(
        "/api/devices/bulk/exit-maintenance",
        json={"device_ids": ids},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["failed"] == 1
    assert data["succeeded"] == 0
