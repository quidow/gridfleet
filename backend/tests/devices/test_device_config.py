from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.settings.service_config import SettingsConfigService
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

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


async def test_merge_config(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    device = await _create_device(db_session, default_host_id)
    await client.patch(
        f"/api/devices/{device['id']}/config",
        json={"a": 1, "b": 2},
    )
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


async def test_get_config_returns_sensitive_values_verbatim(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(db_session, default_host_id)
    patch_resp = await client.patch(
        f"/api/devices/{device['id']}/config",
        json={"api_key": "super-secret", "timeout": 30},
    )
    assert patch_resp.status_code == 200
    resp = await client.get(f"/api/devices/{device['id']}/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["api_key"] == "super-secret"
    assert data["timeout"] == 30


async def test_config_filter_keys(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    device = await _create_device(db_session, default_host_id)
    await client.patch(
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
    await client.patch(f"/api/devices/{device['id']}/config", json={"v": 1})
    await client.patch(f"/api/devices/{device['id']}/config", json={"v": 2})

    resp = await client.get(f"/api/devices/{device['id']}/config/history")
    assert resp.status_code == 200
    history = resp.json()
    assert len(history) == 2


async def test_merge_device_config_coerces_bool_fields(db_session: AsyncSession, default_host_id: str) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="CFG-BOOL-001",
        connection_target="CFG-BOOL-001",
        name="Config Bool Device",
        pack_id=DEVICE_PAYLOAD["pack_id"],
        platform_id=DEVICE_PAYLOAD["platform_id"],
        identity_scheme=DEVICE_PAYLOAD["identity_scheme"],
        identity_scope=DEVICE_PAYLOAD["identity_scope"],
        os_version="14",
    )
    resolved = Mock()
    resolved.device_fields_schema = [{"id": "prefer_devicectl", "type": "bool"}]
    service = SettingsConfigService(publisher=Mock())
    with (
        patch("app.settings.service_config.resolve_pack_for_device", return_value=("p", "plat")),
        patch("app.settings.service_config.resolve_pack_platform", AsyncMock(return_value=resolved)),
    ):
        result = await service.merge_device_config(db_session, device, {"prefer_devicectl": "true"})
    assert result["prefer_devicectl"] is True
