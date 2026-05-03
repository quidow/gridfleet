from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import (
    ConnectionType,
    Device,
    DeviceAvailabilityStatus,
    DeviceType,
)
from app.models.host import Host
from app.models.session import Session, SessionStatus
from app.services import session_service
from app.services.lifecycle_policy import handle_health_failure
from tests.helpers import create_device_record

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_update_session_status_restores_busy_device_when_last_session_finishes(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="android-stale-busy",
        connection_target="android-stale-busy",
        name="Android stale-busy",
        os_version="14",
        availability_status="busy",
    )

    session = Session(session_id="android-sess-1", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    device.verified_at = datetime.now(UTC)
    await db_session.commit()

    updated = await session_service.update_session_status(db_session, "android-sess-1", SessionStatus.passed)

    assert updated is not None
    assert updated.status == SessionStatus.passed
    assert updated.ended_at is not None

    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.available


async def test_update_session_status_preserves_busy_when_another_session_is_running(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="busy-multi-session",
        connection_target="busy-multi-session",
        name="Busy Multi Session",
        os_version="14",
        availability_status="busy",
    )

    db_session.add_all(
        [
            Session(session_id="sess-a", device_id=device.id, status=SessionStatus.running),
            Session(session_id="sess-b", device_id=device.id, status=SessionStatus.running),
        ]
    )
    await db_session.commit()

    updated = await session_service.update_session_status(db_session, "sess-a", SessionStatus.failed)

    assert updated is not None
    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.busy


async def test_update_session_status_restores_reserved_when_active_run_owns_device(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    from tests.helpers import create_reserved_run

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="reserved-session-device",
        connection_target="reserved-session-device",
        name="Reserved Session Device",
        os_version="14",
        availability_status="busy",
    )
    device.verified_at = datetime.now(UTC)
    await db_session.commit()

    await create_reserved_run(db_session, name="Reserved Session Run", devices=[device])

    session = Session(session_id="reserved-sess", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    updated = await session_service.update_session_status(db_session, "reserved-sess", SessionStatus.error)

    assert updated is not None
    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.reserved

    result = await db_session.execute(select(Session).where(Session.session_id == "reserved-sess"))
    stored = result.scalar_one()
    assert stored.ended_at is not None


async def test_update_session_status_clears_stop_pending(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-stuck-stop-1",
        connection_target="policy-stuck-stop-1",
        name="Stuck Deferred Stop Device",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    session = Session(
        session_id="sess-stuck-stop-1",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    result = await handle_health_failure(db_session, device, source="device_checks", reason="ADB not responsive")
    assert result == "deferred"
    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state["stop_pending"] is True

    updated = await session_service.update_session_status(db_session, "sess-stuck-stop-1", SessionStatus.passed)
    assert updated is not None
    assert updated.ended_at is not None

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False, (
        "update_session_status must clear stop_pending after the last session ends"
    )


async def test_register_session_with_terminal_status_clears_stop_pending(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-stuck-stop-2",
        connection_target="policy-stuck-stop-2",
        name="Stuck Deferred Stop Device 2",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    running = Session(
        session_id="sess-stuck-stop-2-running",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(running)
    await db_session.commit()

    result = await handle_health_failure(db_session, device, source="device_checks", reason="ADB not responsive")
    assert result == "deferred"

    # Simulate the running session being closed out-of-band, then a fresh terminal-status
    # registration arriving via testkit's error-session reporting path.
    running.status = SessionStatus.error
    running.ended_at = datetime.now(UTC)
    await db_session.commit()

    await session_service.register_session(
        db_session,
        session_id="sess-stuck-stop-2-error",
        test_name="error-session",
        device_id=device.id,
        status=SessionStatus.error,
        error_type="driver_init_failed",
        error_message="boom",
    )

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False
