from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import create_device_record

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")

DEVICE_PAYLOAD = {
    "identity_value": "CFG-001",
    "connection_target": "CFG-001",
    "name": "Config Test Device",
    "pack_id": "appium-uiautomator2",
    "platform_id": "android_mobile",
    "identity_scheme": "android_serial",
    "identity_scope": "host",
    "os_version": "14",
}


async def _create_device(db_session: AsyncSession, host_id: str) -> dict[str, Any]:
    device = await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=DEVICE_PAYLOAD["identity_value"],
        connection_target=DEVICE_PAYLOAD["connection_target"],
        name=DEVICE_PAYLOAD["name"],
        pack_id=DEVICE_PAYLOAD["pack_id"],
        platform_id=DEVICE_PAYLOAD["platform_id"],
        identity_scheme=DEVICE_PAYLOAD["identity_scheme"],
        identity_scope=DEVICE_PAYLOAD["identity_scope"],
        os_version=DEVICE_PAYLOAD["os_version"],
    )
    return {"id": str(device.id)}


async def test_get_config_empty(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    device = await _create_device(db_session, default_host_id)
    resp = await client.get(f"/api/devices/{device['id']}/config")
    assert resp.status_code == 200
    assert resp.json() == {}


async def test_replace_config(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    device = await _create_device(db_session, default_host_id)
    config = {"newCommandTimeout": 300, "noReset": True}
    resp = await client.put(f"/api/devices/{device['id']}/config", json=config)
    assert resp.status_code == 200
    assert resp.json()["newCommandTimeout"] == 300
    detail_resp = await client.get(f"/api/devices/{device['id']}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["verified_at"] is None
    assert detail_resp.json()["readiness_state"] == "verification_required"


async def test_merge_config(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    device = await _create_device(db_session, default_host_id)
    # Set initial config
    await client.put(
        f"/api/devices/{device['id']}/config",
        json={"a": 1, "b": 2},
    )
    # Merge partial
    resp = await client.patch(
        f"/api/devices/{device['id']}/config",
        json={"b": 99, "c": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["a"] == 1
    assert data["b"] == 99
    assert data["c"] == 3
    detail_resp = await client.get(f"/api/devices/{device['id']}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["verified_at"] is None


async def test_config_masks_sensitive_keys(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    device = await _create_device(db_session, default_host_id)
    await client.put(
        f"/api/devices/{device['id']}/config",
        json={"api_key": "super-secret", "timeout": 30},
    )
    resp = await client.get(f"/api/devices/{device['id']}/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["api_key"] == "********"
    assert data["timeout"] == 30


async def test_config_reveal_sensitive(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    device = await _create_device(db_session, default_host_id)
    await client.put(
        f"/api/devices/{device['id']}/config",
        json={"api_key": "super-secret"},
    )
    resp = await client.get(f"/api/devices/{device['id']}/config?reveal=true")
    assert resp.status_code == 200
    assert resp.json()["api_key"] == "super-secret"


async def test_config_filter_keys(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    device = await _create_device(db_session, default_host_id)
    await client.put(
        f"/api/devices/{device['id']}/config",
        json={"a": 1, "b": 2, "c": 3},
    )
    resp = await client.get(f"/api/devices/{device['id']}/config?keys=a,c")
    assert resp.status_code == 200
    data = resp.json()
    assert "a" in data
    assert "c" in data
    assert "b" not in data


async def test_config_history(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    device = await _create_device(db_session, default_host_id)
    await client.put(f"/api/devices/{device['id']}/config", json={"v": 1})
    await client.put(f"/api/devices/{device['id']}/config", json={"v": 2})

    resp = await client.get(f"/api/devices/{device['id']}/config/history")
    assert resp.status_code == 200
    history = resp.json()
    assert len(history) == 2
