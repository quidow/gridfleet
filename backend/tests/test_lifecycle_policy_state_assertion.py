"""Verify write_state rejects transient device objects."""

import pytest

from app.models.device import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.services.lifecycle_policy_state import write_state


def _transient_device() -> Device:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="transient-lifecycle",
        connection_target="transient-lifecycle",
        name="Transient Lifecycle",
        os_version="14",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    device.operational_state = DeviceOperationalState.available
    device.lifecycle_policy_state = {}
    return device


def test_write_state_rejects_transient_device() -> None:
    device = _transient_device()

    with pytest.raises(AssertionError, match="must be persistent in a session"):
        write_state(device, {"last_action": "test"})
