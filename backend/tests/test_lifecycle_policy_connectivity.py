"""D1: connectivity loss must NOT exclude device from its active run."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import ConnectionType, Device, DeviceHold, DeviceOperationalState, DeviceType
from app.models.host import Host
from app.models.test_run import RunState, TestRun
from app.services import device_locking, lifecycle_policy, run_reservation_service

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_connectivity_loss_keeps_device_in_run(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """note_connectivity_loss must NOT mark the reservation entry excluded."""
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="conn-loss-d1-1",
        connection_target="conn-loss-d1-1",
        name="Connectivity Loss D1 Device",
        os_version="14",
        host_id=db_host.id,
        hold=DeviceHold.reserved,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    run = TestRun(
        name="Active Run D1",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
                "excluded": False,
                "exclusion_reason": None,
                "excluded_at": None,
            }
        ],
    )
    db_session.add(run)
    await db_session.commit()

    locked = await device_locking.lock_device(db_session, device.id)
    await lifecycle_policy.note_connectivity_loss(db_session, locked, reason="Heartbeat timeout")
    await db_session.commit()

    # Reservation entry must still be active (not excluded).
    fresh_run, entry = await run_reservation_service.get_device_reservation_with_entry(db_session, device.id)
    assert fresh_run is not None, "Run reservation must still exist"
    assert fresh_run.id == run.id
    assert entry is not None, "Reservation entry must still exist"
    assert run_reservation_service.reservation_entry_is_excluded(entry) is False, (
        "note_connectivity_loss must NOT exclude the device from its active run"
    )
