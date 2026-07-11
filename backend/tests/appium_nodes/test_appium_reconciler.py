import uuid

from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services.reconciler_agent import build_agent_start_payload
from app.devices.models import ConnectionType, Device, DeviceType


def test_build_agent_start_payload_includes_orchestration_metadata() -> None:
    run_id = uuid.uuid4()
    device = Device(
        id=uuid.uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="serial-1",
        connection_target="serial-1",
        name="Pixel",
        os_version="14",
        host_id=uuid.uuid4(),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    device.appium_node = AppiumNode(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=False,
        stop_pending=True,
        desired_grid_run_id=run_id,
    )
    from tests.fakes import FakeSettingsReader

    payload = build_agent_start_payload(
        device,
        4723,
        settings=FakeSettingsReader({}),
    )

    assert payload["accepting_new_sessions"] is False
    assert payload["stop_pending"] is True
    assert payload["grid_run_id"] == str(run_id)
    # The node-start contract no longer carries stereotype_caps; the relay
    # builds its hub slots from extra_caps. Routing-suppression goes through
    # Selenium NodeStatus.availability.
    assert "stereotype_caps" not in payload
