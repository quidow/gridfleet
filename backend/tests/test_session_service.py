from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import (
    ConnectionType,
    Device,
    DeviceHold,
    DeviceOperationalState,
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
        operational_state="busy",
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
    assert device.operational_state == DeviceOperationalState.available


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
        operational_state="busy",
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
    assert device.operational_state == DeviceOperationalState.busy


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
        operational_state="busy",
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
    assert device.hold == DeviceHold.reserved

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
        operational_state=DeviceOperationalState.busy,
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
        operational_state=DeviceOperationalState.busy,
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


async def test_update_session_status_clears_stop_pending_on_non_busy_device(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Non-busy availability must not gate deferred-stop cleanup.

    A concurrent operator action (or background loop) can move the device
    out of ``busy`` while the running Session row still exists. When that
    session is patched terminal, ``update_session_status`` must still run the
    lifecycle helper so a stale ``stop_pending`` does not survive the
    session-end. The previous gate on ``availability == busy`` skipped this
    case (see audit P1 / CodeAnt comment on PR 64).
    """
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-stuck-stop-non-busy",
        connection_target="policy-stuck-stop-non-busy",
        name="Stuck Stop Non-Busy",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    session = Session(
        session_id="sess-stuck-stop-non-busy",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    result = await handle_health_failure(db_session, device, source="device_checks", reason="ADB hung")
    assert result == "deferred"

    # Simulate an operator (or another loop) flipping the device into
    # maintenance while the session row is still ``running``.
    await db_session.refresh(device)
    device.hold = DeviceHold.maintenance
    await db_session.commit()

    updated = await session_service.update_session_status(db_session, "sess-stuck-stop-non-busy", SessionStatus.passed)
    assert updated is not None

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False, (
        "update_session_status must clear stop_pending even when device availability is no longer busy"
    )


async def test_register_session_running_returns_existing_on_conflict(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Concurrent registrants for the same ``session_id`` must converge.

    The partial unique index ``ux_sessions_session_id_running`` blocks a
    second running insert; ``register_session`` must surface the winner's
    row instead of crashing on IntegrityError.
    """
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="android-conflict",
        connection_target="conflict-target",
        name="Conflict Phone",
    )
    await db_session.commit()

    first = await session_service.register_session(
        db_session,
        session_id="sess-conflict",
        test_name="first",
        device_id=device.id,
        connection_target="conflict-target",
    )
    assert first.test_name == "first"

    # The pre-check returns the existing row for the same session_id, so the
    # ON CONFLICT path is exercised by clearing it from the session cache and
    # invoking register_session again with different metadata. The conflict
    # must short-circuit and return the original row, not raise.
    db_session.expunge_all()
    second = await session_service.register_session(
        db_session,
        session_id="sess-conflict",
        test_name="second",
        device_id=device.id,
        connection_target="conflict-target",
    )
    assert second.test_name == "first"
    assert second.id == first.id

    rows = (await db_session.execute(select(Session).where(Session.session_id == "sess-conflict"))).scalars().all()
    assert len(rows) == 1
