import uuid

import pytest

from app.models.device import ConnectionType, Device, DeviceType
from app.schemas.device import DevicePatch, DeviceVerificationCreate, DeviceVerificationUpdate
from app.services import device_write


def test_platform_defaults_and_shape_validation_branches() -> None:
    assert device_write._platform_defaults(
        platform_id="p",
        device_type=None,
        connection_type=None,
        connection_behavior={"default_device_type": "emulator"},
    ) == (DeviceType.emulator, ConnectionType.virtual)
    assert device_write._platform_defaults(
        platform_id="p",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        connection_behavior={"_allowed_device_types": ["emulator"]},
    ) == (DeviceType.emulator, ConnectionType.usb)
    with pytest.raises(ValueError, match="Device type"):
        device_write._platform_defaults(
            platform_id="p",
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            connection_behavior={"_allowed_device_types": ["emulator", "simulator"]},
        )
    with pytest.raises(ValueError, match="Virtual connection"):
        device_write._platform_defaults(
            platform_id="p",
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.virtual,
            connection_behavior={"_allowed_connection_types": ["usb"]},
        )
    with pytest.raises(ValueError, match="Connection type"):
        device_write._platform_defaults(
            platform_id="p",
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.network,
            connection_behavior={"_allowed_connection_types": ["usb", "virtual"]},
        )

    with pytest.raises(ValueError, match="Assigned host"):
        device_write._validate_device_shape(
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            identity_value="serial",
            connection_target="serial",
            ip_address=None,
            host_id=None,
        )
    with pytest.raises(ValueError, match="IP address"):
        device_write._validate_device_shape(
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.network,
            identity_value="stable",
            connection_target="10.0.0.1:5555",
            ip_address=None,
            host_id=uuid.uuid4(),
        )
    with pytest.raises(ValueError, match="Connection target"):
        device_write._validate_device_shape(
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            identity_value="serial",
            connection_target=None,
            ip_address=None,
            host_id=uuid.uuid4(),
        )
    with pytest.raises(ValueError, match="Emulators"):
        device_write._validate_device_shape(
            device_type=DeviceType.emulator,
            connection_type=ConnectionType.usb,
            identity_value="avd",
            connection_target="avd",
            ip_address=None,
            host_id=uuid.uuid4(),
        )
    with pytest.raises(ValueError, match="Identity value"):
        device_write._validate_device_shape(
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            identity_value=None,
            connection_target="serial",
            ip_address=None,
            host_id=uuid.uuid4(),
        )
    with pytest.raises(ValueError, match="stable identity"):
        device_write._validate_device_shape(
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.network,
            identity_value="10.0.0.1:5555",
            connection_target="10.0.0.1:5555",
            ip_address="10.0.0.1",
            host_id=uuid.uuid4(),
        )
    device_write._validate_device_shape(
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        identity_value=None,
        connection_target=None,
        ip_address=None,
        host_id=uuid.uuid4(),
        connection_behavior={"requires_connection_target": False, "requires_ip_address": False},
    )


def test_device_config_identity_and_create_payload_branches() -> None:
    assert device_write._build_device_config_sync(
        existing_config={"canonical_identity": "old", "keep": True},
        payload_config={"new": True},
    ) == {"keep": True, "new": True}
    assert (
        device_write._build_device_config_sync(
            existing_config={"keep": True},
            payload_config=None,
            replace_config=True,
        )
        == {}
    )

    normalized = {
        "identity_scheme": "serial",
        "identity_value": "stable",
        "connection_target": "",
        "ip_address": "",
    }
    assert device_write._resolve_identity(
        platform_id="android",
        identity_scheme=None,
        identity_value=None,
        connection_target=None,
        ip_address=None,
        device_type=DeviceType.real_device,
        normalized=normalized,
    ) == ("serial", "stable", "", None)

    generated = device_write._resolve_identity(
        platform_id="android",
        identity_scheme=None,
        identity_value=None,
        connection_target=None,
        ip_address=None,
        device_type=DeviceType.real_device,
    )
    assert generated[0] == "manager_generated"
    assert generated[1].startswith("android:")

    with pytest.raises(ValueError, match="platform_id"):
        device_write._resolve_create_payload_fields(
            DeviceVerificationCreate(
                pack_id="pack",
                platform_id="",
                identity_scope="host",
                identity_value="serial",
                connection_target="serial",
                name="name",
                host_id=uuid.uuid4(),
            )
        )
    with pytest.raises(ValueError, match="pack_id"):
        device_write._resolve_create_payload_fields(
            DeviceVerificationCreate(
                pack_id="",
                platform_id="android",
                identity_scope="host",
                identity_value="serial",
                connection_target="serial",
                name="name",
                host_id=uuid.uuid4(),
            )
        )
    with pytest.raises(ValueError, match="identity_scope"):
        device_write._resolve_create_payload_fields(
            DeviceVerificationCreate(
                pack_id="pack",
                platform_id="android",
                identity_value="serial",
                connection_target="serial",
                name="name",
                host_id=uuid.uuid4(),
            )
        )


def test_patch_contract_and_update_payload_branches() -> None:
    device = Device(
        id=uuid.uuid4(),
        pack_id="pack",
        platform_id="android",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="serial",
        connection_target="serial",
        name="Device",
        os_version="14",
        host_id=uuid.uuid4(),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        device_config={"canonical_identity": "old", "keep": True},
    )
    with pytest.raises(ValueError, match="connection target"):
        device_write.validate_patch_contract(device, DevicePatch(connection_target="new"))
    with pytest.raises(ValueError, match="IP address"):
        device_write.validate_patch_contract(device, DevicePatch(ip_address="10.0.0.1"))

    payload = device_write.prepare_device_update_payload(
        device,
        DeviceVerificationUpdate(
            host_id=device.host_id,
            connection_target="10.0.0.2:5555",
            identity_value="stable",
            connection_type=ConnectionType.network,
            ip_address="10.0.0.2",
            replace_device_config=True,
            device_config={"fresh": True},
        ),
    )
    assert payload["ip_address"] == "10.0.0.2"
    assert payload["device_config"] == {"fresh": True}

    virtual_payload = device_write.prepare_device_update_payload(
        device,
        DeviceVerificationUpdate(
            host_id=device.host_id,
            identity_value="avd:Pixel",
            connection_target="Pixel",
            device_type=DeviceType.emulator,
            connection_type=ConnectionType.virtual,
            ip_address="10.0.0.3",
        ),
    )
    assert virtual_payload["ip_address"] is None
