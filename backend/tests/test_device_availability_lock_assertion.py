"""Verify device state writers reject transient device objects."""

import pytest

from app.models.device import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.services.device_state import set_operational_state

pytestmark = pytest.mark.asyncio


def _transient_device() -> Device:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="transient-availability",
        connection_target="transient-availability",
        name="Transient Availability",
        os_version="14",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    device.operational_state = DeviceOperationalState.available
    return device


async def test_set_operational_state_rejects_transient_device() -> None:
    """A Device that is not persistent in a session should trigger an assertion."""
    device = _transient_device()

    with pytest.raises(AssertionError, match="must be persistent in a session"):
        await set_operational_state(
            device,
            DeviceOperationalState.offline,
            publish_event=False,
        )
