from __future__ import annotations

import io
import tarfile
import uuid
from typing import TYPE_CHECKING

import pytest

from app.config import settings
from app.models.device import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.services.config_service import MASK_VALUE
from app.services.pack_storage_service import PackStorageService
from app.services.pack_upload_service import upload_pack
from tests.pack.factories import seed_test_packs

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _driver_pack_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "driver_pack_storage_dir", tmp_path / "driver-packs")


def _tarball(manifest: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        body = manifest.encode()
        info = tarfile.TarInfo(name="manifest.yaml")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


async def _make_roku_device(db: AsyncSession, host: Host) -> Device:
    device = Device(
        id=uuid.uuid4(),
        name="Living Room Roku",
        pack_id="appium-roku-dlenroc",
        platform_id="roku_network",
        identity_scheme="roku_serial",
        identity_scope="global",
        identity_value="roku-123",
        connection_target=None,
        os_version="12.5",
        host_id=host.id,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        ip_address="192.0.2.44",
        device_config={"roku_password": "super-secret", "pin": "2468", "label": "den"},
    )
    db.add(device)
    await db.commit()
    await db.refresh(device)
    return device


async def test_device_list_masks_manifest_sensitive_config(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()
    device = await _make_roku_device(db_session, db_host)

    res = await client.get("/api/devices", params={"limit": 100})
    assert res.status_code == 200
    item = next(d for d in res.json()["items"] if d["id"] == str(device.id))
    assert item["device_config"]["roku_password"] == MASK_VALUE
    assert item["device_config"]["label"] == "den"


async def test_device_config_endpoint_masks_by_manifest_sensitive_flag(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()
    device = await _make_roku_device(db_session, db_host)

    res = await client.get(f"/api/devices/{device.id}/config")
    assert res.status_code == 200
    body = res.json()
    assert body["roku_password"] == MASK_VALUE
    assert body["label"] == "den"


async def test_manifest_sensitive_non_regex_key_is_masked(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
    tmp_path: Path,
) -> None:
    manifest = """\
schema_version: 1
id: vendor-pin-driver
release: 0.1.0
display_name: PIN Driver
appium_server:
  source: npm
  package: appium
  version: ">=2.5,<3"
  recommended: 2.19.0
appium_driver:
  source: npm
  package: appium-pin-driver
  version: ">=1,<2"
  recommended: 1.0.0
platforms:
  - id: pin_device
    display_name: PIN Device
    automation_name: Pin
    appium_platform_name: PinOS
    device_types: [real_device]
    connection_types: [network]
    grid_slots: [native]
    capabilities:
      stereotype: {}
      session_required: []
    identity:
      scheme: pin_serial
      scope: global
    device_fields_schema:
      - id: pin
        label: PIN
        type: string
        sensitive: true
        required_for_session: true
        capability_name: "appium:pin"
"""
    await upload_pack(
        db_session,
        storage=PackStorageService(root=tmp_path),
        username="test",
        origin_filename="vendor-pin-driver-0.1.0.tar.gz",
        data=_tarball(manifest),
    )
    device = Device(
        id=uuid.uuid4(),
        name="PIN device",
        pack_id="vendor-pin-driver",
        platform_id="pin_device",
        identity_scheme="pin_serial",
        identity_scope="global",
        identity_value="pin-123",
        os_version="1",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        device_config={"pin": "2468", "label": "safe"},
    )
    db_session.add(device)
    await db_session.commit()

    res = await client.get(f"/api/devices/{device.id}")
    assert res.status_code == 200
    assert res.json()["device_config"]["pin"] == MASK_VALUE
    assert res.json()["device_config"]["label"] == "safe"
