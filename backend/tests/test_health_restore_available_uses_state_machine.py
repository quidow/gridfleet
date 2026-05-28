"""Regression: _restore_available_for_healthy_signal must route through DeviceStateMachine
(CONNECTIVITY_RESTORED transition), not call set_operational_state directly. Routing
through the state machine fires EventLogHook which writes a DeviceEvent row;
the direct write emitted a bus event but did NOT add this ORM row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.models.event import DeviceEvent, DeviceEventType
from app.devices.services import state_write_guard
from app.devices.services.health import _restore_available_for_healthy_signal
from tests.helpers import test_event_bus as event_bus

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
    verified_at: datetime | None = None,
    device_checks_healthy: bool | None = None,
) -> Device:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value=identity_value,
            connection_target=identity_value,
            name=f"Restore-Available Test Device ({identity_value})",
            os_version="14",
            host_id=db_host.id,
            operational_state=operational_state,
            verified_at=verified_at,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            device_checks_healthy=device_checks_healthy,
        )
    db_session.add(device)
    await db_session.commit()
    return device


async def _add_running_node(db_session: AsyncSession, device: Device) -> AppiumNode:
    """Add an AppiumNode that satisfies node_running_signal (pid + active_connection_target)."""
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://h",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=1,
            active_connection_target=device.connection_target,
        )
    db_session.add(node)
    await db_session.commit()
    return node


@pytest.mark.db
@pytest.mark.asyncio
async def test_healthy_signal_on_offline_emits_connectivity_restored(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """After conversion, a healthy signal on an eligible offline device must go
    through DeviceStateMachine so EventLogHook writes a DeviceEvent row with
    event_type=connectivity_restored. The direct set_operational_state write
    emitted a bus event but did NOT add this ORM row.
    """
    device = await _make_device(
        db_session,
        db_host,
        operational_state=DeviceOperationalState.offline,
        identity_value="restore-avail-sm-001",
        verified_at=datetime.now(UTC),
    )
    await _add_running_node(db_session, device)

    # Reload to get a fresh instance in the session; lock is acquired inside the helper.
    loaded = await db_session.get(Device, device.id)
    assert loaded is not None
    await db_session.refresh(loaded, attribute_names=["appium_node"])

    await _restore_available_for_healthy_signal(db_session, loaded, publisher=Mock())
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == loaded.id,
                    DeviceEvent.event_type == DeviceEventType.connectivity_restored,
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(rows) == 1, (
        "Expected exactly one DeviceEvent row with event_type=connectivity_restored; "
        f"got {len(rows)}. This means _restore_available_for_healthy_signal did not go "
        "through DeviceStateMachine."
    )

    row = rows[0]
    assert row.details is not None
    assert row.details.get("from") == "offline/None", (
        f"Expected 'from' to be 'offline/None', got {row.details.get('from')!r}"
    )
    assert row.details.get("to") == "available/None", (
        f"Expected 'to' to be 'available/None', got {row.details.get('to')!r}"
    )

    # Verify the device is now available
    await db_session.refresh(loaded)
    assert loaded.operational_state == DeviceOperationalState.available


@pytest.mark.db
@pytest.mark.asyncio
async def test_healthy_signal_not_offline_emits_no_transition(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Guard: device not in offline state — must short-circuit, no DeviceEvent."""
    device = await _make_device(
        db_session,
        db_host,
        operational_state=DeviceOperationalState.available,
        identity_value="restore-avail-sm-002",
        verified_at=datetime.now(UTC),
    )
    await _add_running_node(db_session, device)

    loaded = await db_session.get(Device, device.id)
    assert loaded is not None
    await db_session.refresh(loaded, attribute_names=["appium_node"])

    await _restore_available_for_healthy_signal(db_session, loaded, event_bus)
    await db_session.commit()

    rows = (await db_session.execute(select(DeviceEvent).where(DeviceEvent.device_id == loaded.id))).scalars().all()
    assert len(rows) == 0, f"Expected no DeviceEvent rows (not offline); got {len(rows)}."

    await db_session.refresh(loaded)
    assert loaded.operational_state == DeviceOperationalState.available


@pytest.mark.db
@pytest.mark.asyncio
async def test_healthy_signal_node_not_running_emits_no_transition(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Guard: node not running (health_running=False) — must short-circuit, no DeviceEvent."""
    device = await _make_device(
        db_session,
        db_host,
        operational_state=DeviceOperationalState.offline,
        identity_value="restore-avail-sm-004",
        verified_at=datetime.now(UTC),
    )
    # Add a node that is explicitly not running
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://h",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            health_running=False,
        )
    db_session.add(node)
    await db_session.commit()

    loaded = await db_session.get(Device, device.id)
    assert loaded is not None
    await db_session.refresh(loaded, attribute_names=["appium_node"])

    await _restore_available_for_healthy_signal(db_session, loaded, event_bus)
    await db_session.commit()

    rows = (await db_session.execute(select(DeviceEvent).where(DeviceEvent.device_id == loaded.id))).scalars().all()
    assert len(rows) == 0, f"Expected no DeviceEvent rows (node not running); got {len(rows)}."

    await db_session.refresh(loaded)
    assert loaded.operational_state == DeviceOperationalState.offline


@pytest.mark.db
@pytest.mark.asyncio
async def test_healthy_signal_no_node_emits_no_transition(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Guard: no appium_node — must short-circuit, no DeviceEvent."""
    device = await _make_device(
        db_session,
        db_host,
        operational_state=DeviceOperationalState.offline,
        identity_value="restore-avail-sm-005",
        verified_at=datetime.now(UTC),
    )
    # No node added.

    loaded = await db_session.get(Device, device.id)
    assert loaded is not None
    await db_session.refresh(loaded, attribute_names=["appium_node"])

    await _restore_available_for_healthy_signal(db_session, loaded, event_bus)
    await db_session.commit()

    rows = (await db_session.execute(select(DeviceEvent).where(DeviceEvent.device_id == loaded.id))).scalars().all()
    assert len(rows) == 0, f"Expected no DeviceEvent rows (no node); got {len(rows)}."

    await db_session.refresh(loaded)
    assert loaded.operational_state == DeviceOperationalState.offline


@pytest.mark.db
@pytest.mark.asyncio
async def test_healthy_signal_not_ready_emits_no_transition(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Guard: is_ready_for_use_async returns False (verified_at=None) — no DeviceEvent."""
    device = await _make_device(
        db_session,
        db_host,
        operational_state=DeviceOperationalState.offline,
        identity_value="restore-avail-sm-006",
        verified_at=None,  # not verified → is_ready_for_use_async returns False
    )
    await _add_running_node(db_session, device)

    loaded = await db_session.get(Device, device.id)
    assert loaded is not None
    await db_session.refresh(loaded, attribute_names=["appium_node"])

    await _restore_available_for_healthy_signal(db_session, loaded, event_bus)
    await db_session.commit()

    rows = (await db_session.execute(select(DeviceEvent).where(DeviceEvent.device_id == loaded.id))).scalars().all()
    assert len(rows) == 0, f"Expected no DeviceEvent rows (not ready); got {len(rows)}."

    await db_session.refresh(loaded)
    assert loaded.operational_state == DeviceOperationalState.offline


@pytest.mark.db
@pytest.mark.asyncio
async def test_healthy_signal_allocation_not_allowed_emits_no_transition(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Guard: device_allows_allocation returns False (device_checks_healthy=False) — no DeviceEvent."""
    device = await _make_device(
        db_session,
        db_host,
        operational_state=DeviceOperationalState.offline,
        identity_value="restore-avail-sm-007",
        verified_at=datetime.now(UTC),
        device_checks_healthy=False,  # device_allows_allocation returns False
    )
    await _add_running_node(db_session, device)

    loaded = await db_session.get(Device, device.id)
    assert loaded is not None
    await db_session.refresh(loaded, attribute_names=["appium_node"])

    await _restore_available_for_healthy_signal(db_session, loaded, event_bus)
    await db_session.commit()

    rows = (await db_session.execute(select(DeviceEvent).where(DeviceEvent.device_id == loaded.id))).scalars().all()
    assert len(rows) == 0, f"Expected no DeviceEvent rows (allocation not allowed); got {len(rows)}."

    await db_session.refresh(loaded)
    assert loaded.operational_state == DeviceOperationalState.offline
