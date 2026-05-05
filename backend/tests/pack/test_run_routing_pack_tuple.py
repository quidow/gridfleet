from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.models.host import Host
from app.schemas.run import DeviceRequirement
from app.services.run_service import _find_matching_devices

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_find_matching_devices_matches_pack_tuple(db_session: AsyncSession, db_host: Host) -> None:
    now = datetime.now(UTC)
    android = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="serial-1",
        connection_target="serial-1",
        name="Android",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        verified_at=now,
    )
    ios = Device(
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="apple_udid",
        identity_scope="global",
        identity_value="ios-1",
        connection_target="ios-1",
        name="iPhone",
        os_version="17",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        device_config={"bundle_id": "com.example.app"},
        verified_at=now,
    )
    db_session.add_all([android, ios])
    await db_session.flush()

    matches = await _find_matching_devices(
        db_session,
        DeviceRequirement(pack_id="appium-xcuitest", platform_id="ios", count=1),
    )

    assert [device.id for device in matches] == [ios.id]
