import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import Device, DeviceHold, DeviceOperationalState
from app.devices.services.lifecycle_state_machine import DeviceStateMachine
from app.devices.services.lifecycle_state_machine_types import TransitionEvent
from app.hosts.models import Host

pytestmark = [pytest.mark.db]


async def test_full_lifecycle_via_state_machine(db_session: AsyncSession, db_host: Host) -> None:
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
        hold=None,
        device_type="real_device",
        connection_type="usb",
    )
    db_session.add(device)
    await db_session.flush()

    machine = DeviceStateMachine()

    await machine.transition(device, TransitionEvent.SESSION_STARTED, reason="run start")
    assert device.operational_state == DeviceOperationalState.busy

    await machine.transition(device, TransitionEvent.SESSION_ENDED, reason="run end")
    assert device.operational_state == DeviceOperationalState.available

    await machine.transition(device, TransitionEvent.CONNECTIVITY_LOST, reason="cable yanked")
    assert device.operational_state == DeviceOperationalState.offline
    assert device.hold is None

    await machine.transition(device, TransitionEvent.MAINTENANCE_ENTERED, reason="operator")
    assert device.operational_state == DeviceOperationalState.offline
    assert device.hold == DeviceHold.maintenance

    # Re-assert is a no-op (idempotent)
    changed = await machine.transition(device, TransitionEvent.MAINTENANCE_ENTERED, reason="operator")
    assert changed is False

    await machine.transition(device, TransitionEvent.MAINTENANCE_EXITED, reason="operator")
    assert device.hold is None
    assert device.operational_state == DeviceOperationalState.offline

    await machine.transition(device, TransitionEvent.CONNECTIVITY_RESTORED, reason="recovered")
    assert device.operational_state == DeviceOperationalState.available


async def test_reserved_hold_survives_session_lifecycle(db_session: AsyncSession, db_host: Host) -> None:
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
        hold=DeviceHold.reserved,
        device_type="real_device",
        connection_type="usb",
    )
    db_session.add(device)
    await db_session.flush()

    machine = DeviceStateMachine()
    await machine.transition(device, TransitionEvent.SESSION_STARTED)
    assert device.operational_state == DeviceOperationalState.busy
    assert device.hold == DeviceHold.reserved

    await machine.transition(device, TransitionEvent.SESSION_ENDED)
    assert device.operational_state == DeviceOperationalState.available
    assert device.hold == DeviceHold.reserved


async def test_offline_reserved_to_busy_via_session_started(db_session: AsyncSession, db_host: Host) -> None:
    """Production race: session arrives on an offline-but-reserved device before
    the connectivity loop catches up. The machine handles it directly."""
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
        hold=DeviceHold.reserved,
        device_type="real_device",
        connection_type="usb",
    )
    db_session.add(device)
    await db_session.flush()

    machine = DeviceStateMachine()
    await machine.transition(device, TransitionEvent.SESSION_STARTED)
    assert device.operational_state == DeviceOperationalState.busy
    assert device.hold == DeviceHold.reserved
