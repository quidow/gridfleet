from unittest.mock import Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import Device, DeviceOperationalState
from app.devices.services import state_write_guard
from app.devices.services.lifecycle_state_machine import DeviceStateMachine
from app.devices.services.lifecycle_state_machine_types import TransitionEvent
from app.hosts.models import Host

pytestmark = [pytest.mark.db]


async def test_full_lifecycle_via_state_machine(db_session: AsyncSession, db_host: Host) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="sm-integration-1",
            connection_target="sm-integration-1",
            name="SM Integration Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            device_type="real_device",
            connection_type="usb",
        )
    db_session.add(device)
    await db_session.flush()

    machine = DeviceStateMachine()
    publisher = Mock()

    await machine.transition(device, TransitionEvent.SESSION_STARTED, reason="run start", publisher=publisher)
    assert device.operational_state == DeviceOperationalState.busy

    await machine.transition(device, TransitionEvent.SESSION_ENDED, reason="run end", publisher=publisher)
    assert device.operational_state == DeviceOperationalState.available

    await machine.transition(device, TransitionEvent.CONNECTIVITY_LOST, reason="cable yanked", publisher=publisher)
    assert device.operational_state == DeviceOperationalState.offline

    # Re-assert is a no-op (idempotent)
    changed = await machine.transition(
        device, TransitionEvent.CONNECTIVITY_LOST, reason="cable yanked", publisher=publisher
    )
    assert changed is False

    await machine.transition(device, TransitionEvent.CONNECTIVITY_RESTORED, reason="recovered", publisher=publisher)
    assert device.operational_state == DeviceOperationalState.available


async def test_reserved_device_session_lifecycle(db_session: AsyncSession, db_host: Host) -> None:
    """A reserved device (tracked via reservation rows, not hold) cycles through a
    session on the operational axis. The state machine only touches operational_state."""
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="sm-integration-2",
            connection_target="sm-integration-2",
            name="SM Integration Reserved",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            device_type="real_device",
            connection_type="usb",
        )
    db_session.add(device)
    await db_session.flush()

    machine = DeviceStateMachine()
    publisher = Mock()
    await machine.transition(device, TransitionEvent.SESSION_STARTED, publisher=publisher)
    assert device.operational_state == DeviceOperationalState.busy

    await machine.transition(device, TransitionEvent.SESSION_ENDED, publisher=publisher)
    assert device.operational_state == DeviceOperationalState.available


async def test_offline_to_busy_via_session_started(db_session: AsyncSession, db_host: Host) -> None:
    """Production race: session arrives on an offline device before the
    connectivity loop catches up. The machine handles it directly."""
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="sm-integration-3",
            connection_target="sm-integration-3",
            name="SM Integration Race",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.offline,
            device_type="real_device",
            connection_type="usb",
        )
    db_session.add(device)
    await db_session.flush()

    machine = DeviceStateMachine()
    await machine.transition(device, TransitionEvent.SESSION_STARTED, publisher=Mock())
    assert device.operational_state == DeviceOperationalState.busy
