from datetime import UTC, datetime

from app.models.device import ConnectionType, Device, DeviceType
from app.models.host import Host
from app.services.device_readiness import assess_device_from_required_fields


def test_assess_device_from_required_fields_reports_missing_session_field(db_host: Host) -> None:
    device = Device(
        pack_id="appium-roku",
        platform_id="roku_network",
        identity_scheme="roku_serial",
        identity_scope="global",
        identity_value="roku-1",
        connection_target="192.168.1.44",
        name="Living Room Roku",
        os_version="12",
        host_id=db_host.id,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        device_config={},
    )
    fields = [{"id": "roku_password", "required_for_session": True}]

    assessment = assess_device_from_required_fields(device, fields)

    assert assessment.readiness_state == "setup_required"
    assert assessment.missing_setup_fields == ["roku_password"]


def test_assess_required_fields_returns_public_verified_state(db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="serial-1",
        connection_target="serial-1",
        name="Verified Android",
        os_version="14",
        host_id=db_host.id,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        verified_at=datetime.now(UTC),
    )

    assessment = assess_device_from_required_fields(device, [])

    assert assessment.readiness_state == "verified"
