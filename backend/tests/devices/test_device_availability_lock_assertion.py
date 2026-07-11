"""Verify device state writers reject transient device objects."""

import pytest

from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services.state import _persistent_session

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
    device.operational_state_last_emitted = DeviceOperationalState.available
    return device


async def test_edge_detector_requires_a_persistent_device() -> None:
    """A Device that is not persistent in a session cannot queue an edge."""
    device = _transient_device()

    with pytest.raises(AssertionError, match="must be persistent in a session"):
        _persistent_session(device)
