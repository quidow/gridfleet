"""Regression: _mark_offline_for_failed_signal must route through DeviceStateMachine
(CONNECTIVITY_LOST transition), not call set_operational_state directly. Routing
through the state machine fires EventLogHook which writes a DeviceEvent row;
the direct write emitted a bus event but did NOT add this ORM row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.models.event import DeviceEvent, DeviceEventType
from app.devices.services import state_write_guard
from app.devices.services.health import _mark_offline_for_failed_signal

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def _make_device(
    db_session: AsyncSession,
    db_host: Host,
    *,
    operational_state: DeviceOperationalState,
    identity_value: str,
) -> Device:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value=identity_value,
            connection_target=identity_value,
            name=f"Mark-Offline Test Device ({identity_value})",
            os_version="14",
            host_id=db_host.id,
            operational_state=operational_state,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()
    return device


@pytest.mark.db
@pytest.mark.asyncio
async def test_failed_signal_on_available_emits_connectivity_lost(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """After conversion, a failed signal on an available device must go through
    DeviceStateMachine so EventLogHook writes a DeviceEvent row with
    event_type=connectivity_lost. The direct set_operational_state write emitted
    a bus event but did NOT add this ORM row.
    """
    device = await _make_device(
        db_session,
        db_host,
        operational_state=DeviceOperationalState.available,
        identity_value="mark-offline-sm-001",
    )

    # Reload to get a fresh instance in the session; lock is acquired inside the helper.
    loaded = await db_session.get(Device, device.id)
    assert loaded is not None

    await _mark_offline_for_failed_signal(loaded, failed=True, reason="ADB lost")
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == loaded.id,
                    DeviceEvent.event_type == DeviceEventType.connectivity_lost,
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(rows) == 1, (
        "Expected exactly one DeviceEvent row with event_type=connectivity_lost; "
        f"got {len(rows)}. This means _mark_offline_for_failed_signal did not go "
        "through DeviceStateMachine."
    )

    row = rows[0]
    assert row.details is not None
    assert row.details.get("from") == "available/None", (
        f"Expected 'from' to be 'available/None', got {row.details.get('from')!r}"
    )
    assert row.details.get("to") == "offline/None", f"Expected 'to' to be 'offline/None', got {row.details.get('to')!r}"

    # Verify the device is actually offline
    await db_session.refresh(loaded)
    assert loaded.operational_state == DeviceOperationalState.offline


@pytest.mark.db
@pytest.mark.asyncio
async def test_failed_signal_on_offline_emits_no_transition(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A failed signal on a device that is already offline must short-circuit
    before any state-machine call. No DeviceEvent row should be written.
    """
    device = await _make_device(
        db_session,
        db_host,
        operational_state=DeviceOperationalState.offline,
        identity_value="mark-offline-sm-002",
    )

    loaded = await db_session.get(Device, device.id)
    assert loaded is not None

    await _mark_offline_for_failed_signal(loaded, failed=True, reason="ADB lost")
    await db_session.commit()

    rows = (await db_session.execute(select(DeviceEvent).where(DeviceEvent.device_id == loaded.id))).scalars().all()

    assert len(rows) == 0, f"Expected no DeviceEvent rows for an already-offline device; got {len(rows)}."

    # State must remain offline
    await db_session.refresh(loaded)
    assert loaded.operational_state == DeviceOperationalState.offline


@pytest.mark.db
@pytest.mark.asyncio
async def test_unfailed_signal_emits_no_transition(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A non-failed signal (failed=False) must short-circuit immediately and
    produce no DeviceEvent row, regardless of the device's operational state.
    """
    device = await _make_device(
        db_session,
        db_host,
        operational_state=DeviceOperationalState.available,
        identity_value="mark-offline-sm-003",
    )

    loaded = await db_session.get(Device, device.id)
    assert loaded is not None

    await _mark_offline_for_failed_signal(loaded, failed=False, reason="all good")
    await db_session.commit()

    rows = (await db_session.execute(select(DeviceEvent).where(DeviceEvent.device_id == loaded.id))).scalars().all()

    assert len(rows) == 0, f"Expected no DeviceEvent rows when failed=False; got {len(rows)}."

    # State must remain available
    await db_session.refresh(loaded)
    assert loaded.operational_state == DeviceOperationalState.available
