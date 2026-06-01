import uuid
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.appium_nodes.models import AppiumNode
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceReservation, DeviceType
from app.devices.services import state_write_guard
from app.devices.services.lifecycle_policy_actions import LifecyclePolicyActionsService
from app.hosts.models import Host
from app.runs.models import RunState, TestRun
from app.runs.service_reservation import RunReservationService

_actions = LifecyclePolicyActionsService(publisher=Mock(), reservation=RunReservationService())


def _make_device(
    host: Host,
    operational_state: DeviceOperationalState = DeviceOperationalState.available,
) -> Device:
    with state_write_guard.bypass():
        _bypass_tmp = Device(
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
            operational_state=operational_state,
        )
    return _bypass_tmp


@pytest.fixture
async def device_with_active_run(db_session: AsyncSession, db_host: Host) -> tuple[Device, TestRun]:
    """Create a device reserved for an active run."""
    device = _make_device(db_host)
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

    with state_write_guard.bypass():
        db_session.add(
            AppiumNode(
                device_id=device.id,
                port=4723,
                grid_url="http://grid:4444",
                pid=1234,
                active_connection_target=device.connection_target,
                desired_grid_run_id=run.id,
                grid_run_id=run.id,
            )
        )

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


async def test_exclude_run_if_needed_excludes_without_maintenance(
    db_session: AsyncSession,
    device_with_active_run: tuple[Device, TestRun],
) -> None:
    """Auto-paths (health/connectivity failures) must NOT escalate the device
    into maintenance. Only the operator UI, ``report_preparation_failure``,
    and cooldown-threshold escalation are allowed to flip the hold."""
    device, _run = device_with_active_run

    returned_run, entry = await _actions.exclude_run_if_needed(
        db_session, device, reason="Undetectable issue", source="test"
    )

    assert returned_run is not None
    assert entry is not None
    assert entry.excluded is True
    # Auto-exclusion must not silently escalate the device into maintenance.
    assert device.operational_state != DeviceOperationalState.maintenance


async def test_exclude_run_if_needed_no_run_is_noop(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = _make_device(db_host, operational_state=DeviceOperationalState.available)
    db_session.add(device)
    await db_session.commit()

    stmt = select(Device).where(Device.id == device.id).options(selectinload(Device.appium_node))
    result = await db_session.execute(stmt)
    device = result.scalar_one()

    returned_run, _entry = await _actions.exclude_run_if_needed(db_session, device, reason="No run", source="test")

    assert returned_run is None
    assert device.operational_state == DeviceOperationalState.available


async def test_exclude_run_if_needed_idempotent_does_not_flip_to_maintenance(
    db_session: AsyncSession,
    device_with_active_run: tuple[Device, TestRun],
) -> None:
    device, _run = device_with_active_run

    await _actions.exclude_run_if_needed(db_session, device, reason="First issue", source="test")
    assert device.operational_state != DeviceOperationalState.maintenance

    await _actions.exclude_run_if_needed(db_session, device, reason="First issue", source="test")
    assert device.operational_state != DeviceOperationalState.maintenance


async def test_exclude_run_if_needed_clears_desired_grid_run_id(
    db_session: AsyncSession,
    device_with_active_run: tuple[Device, TestRun],
) -> None:
    device, _run = device_with_active_run

    await _actions.exclude_run_if_needed(db_session, device, reason="Node health failed", source="test")
    await db_session.commit()

    await db_session.refresh(device, ["appium_node"])
    assert device.appium_node is not None
    assert device.appium_node.desired_grid_run_id is None


async def test_restore_run_if_needed_restores_desired_grid_run_id(
    db_session: AsyncSession,
    device_with_active_run: tuple[Device, TestRun],
) -> None:
    device, run = device_with_active_run
    returned_run, entry = await _actions.exclude_run_if_needed(
        db_session, device, reason="Node health failed", source="test"
    )
    assert returned_run is not None
    assert entry is not None
    await db_session.refresh(device, ["appium_node"])
    assert device.appium_node is not None
    device.appium_node.desired_grid_run_id = None
    await db_session.commit()

    restored_run, restored_entry = await _actions.restore_run_if_needed(
        db_session,
        device,
        returned_run,
        entry,
        reason="Node recovered",
        source="test",
    )
    await db_session.commit()

    assert restored_run is not None
    assert restored_entry is not None
    assert restored_entry.excluded is False
    await db_session.refresh(device, ["appium_node"])
    assert device.appium_node is not None
    assert device.appium_node.desired_grid_run_id == run.id
