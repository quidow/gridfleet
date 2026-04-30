import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.device import ConnectionType, Device, DeviceAvailabilityStatus, DeviceType
from app.models.device_reservation import DeviceReservation
from app.models.host import Host
from app.models.test_run import RunState, TestRun
from app.services.lifecycle_policy_actions import exclude_run_if_needed


def _make_device(
    host: Host, availability_status: DeviceAvailabilityStatus = DeviceAvailabilityStatus.available
) -> Device:
    return Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=f"TEST{uuid.uuid4().hex[:8].upper()}",
        connection_target="usb-serial",
        name="test-device",
        os_version="14",
        host_id=host.id,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        availability_status=availability_status,
        auto_manage=True,
    )


@pytest.fixture
async def device_with_active_run(db_session: AsyncSession, db_host: Host) -> tuple[Device, TestRun]:
    """Create a device reserved for an active run."""
    device = _make_device(db_host, availability_status=DeviceAvailabilityStatus.reserved)
    db_session.add(device)
    await db_session.flush()

    run = TestRun(
        name="test-run",
        state=RunState.active,
        requirements=[{"platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
    )
    db_session.add(run)
    await db_session.flush()

    reservation = DeviceReservation(
        run=run,
        device_id=device.id,
        identity_value=device.identity_value,
        connection_target=device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        os_version=device.os_version,
        host_ip=None,
        excluded=False,
        exclusion_reason=None,
        excluded_at=None,
    )
    db_session.add(reservation)
    await db_session.commit()

    stmt = select(TestRun).where(TestRun.id == run.id).options(selectinload(TestRun.device_reservations))
    result = await db_session.execute(stmt)
    run = result.scalar_one()

    stmt = select(Device).where(Device.id == device.id).options(selectinload(Device.appium_node))
    result = await db_session.execute(stmt)
    device = result.scalar_one()

    return device, run


async def test_exclude_run_if_needed_enters_maintenance(
    db_session: AsyncSession,
    device_with_active_run: tuple[Device, TestRun],
) -> None:
    device, _run = device_with_active_run

    returned_run, entry = await exclude_run_if_needed(db_session, device, reason="Undetectable issue", source="test")

    assert returned_run is not None
    assert entry is not None
    assert entry.excluded is True
    assert device.availability_status == DeviceAvailabilityStatus.maintenance


async def test_exclude_run_if_needed_no_run_skips_maintenance(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = _make_device(db_host, availability_status=DeviceAvailabilityStatus.available)
    db_session.add(device)
    await db_session.commit()

    stmt = select(Device).where(Device.id == device.id).options(selectinload(Device.appium_node))
    result = await db_session.execute(stmt)
    device = result.scalar_one()

    returned_run, _entry = await exclude_run_if_needed(db_session, device, reason="No run", source="test")

    assert returned_run is None
    assert device.availability_status == DeviceAvailabilityStatus.available


async def test_exclude_run_if_needed_already_excluded_stays_maintenance(
    db_session: AsyncSession,
    device_with_active_run: tuple[Device, TestRun],
) -> None:
    device, _run = device_with_active_run

    await exclude_run_if_needed(db_session, device, reason="First issue", source="test")
    assert device.availability_status == DeviceAvailabilityStatus.maintenance

    await exclude_run_if_needed(db_session, device, reason="First issue", source="test")
    assert device.availability_status == DeviceAvailabilityStatus.maintenance
