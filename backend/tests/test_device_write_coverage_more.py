import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.devices.models import ConnectionType, Device, DeviceType
from app.devices.schemas.device import DevicePatch, DeviceVerificationCreate, DeviceVerificationUpdate
from app.devices.services import write as device_write


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
    assert device_write._platform_defaults(
        platform_id="p",
        device_type=DeviceType.real_device,
        connection_type=None,
        connection_behavior={"default_connection_type": "network"},
    ) == (DeviceType.real_device, ConnectionType.network)
    assert device_write._platform_defaults(
        platform_id="p",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        connection_behavior={"default_device_type": "emulator", "_allowed_device_types": ["emulator"]},
    ) == (DeviceType.emulator, ConnectionType.usb)
    assert device_write._platform_defaults(
        platform_id="p",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        connection_behavior={"_allowed_connection_types": ["usb"]},
    ) == (DeviceType.real_device, ConnectionType.usb)
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
    with pytest.raises(ValueError, match="IP address"):
        device_write._validate_device_shape(
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            identity_value="stable",
            connection_target="stable",
            ip_address=None,
            host_id=uuid.uuid4(),
            connection_behavior={"requires_ip_address": True},
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
        identity_value="10.0.0.1:5555",
        connection_target="10.0.0.1:5555",
        ip_address="10.0.0.1",
        host_id=uuid.uuid4(),
        allow_transport_identity_resolution=True,
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
    assert device_write._is_transport_identity(None, None, None) is True
    assert device_write._is_transport_identity("10.0.0.1", None, None) is True
    assert device_write._is_transport_identity("10.0.0.1:5555", "10.0.0.1:5555", None) is True
    assert device_write._is_transport_identity("stable", "10.0.0.1:5555", "10.0.0.1") is False

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

    prepared = device_write.prepare_device_create_payload(
        DeviceVerificationCreate(
            pack_id="pack",
            platform_id="android",
            identity_scope="host",
            identity_value="avd:Pixel",
            connection_target="Pixel",
            name="Pixel",
            host_id=uuid.uuid4(),
            device_type=DeviceType.emulator,
            connection_type=ConnectionType.virtual,
            ip_address="10.0.0.9",
            device_config={"fresh": True},
        )
    )
    assert prepared["ip_address"] is None
    assert prepared["device_config"] == {"fresh": True}

    normalized_payload = device_write._resolve_create_payload_fields(
        DeviceVerificationCreate(
            pack_id="pack",
            platform_id="android",
            identity_value="input",
            connection_target="input",
            name="normalized",
            host_id=uuid.uuid4(),
        ),
        normalized={
            "identity_scheme": "serial",
            "identity_scope": "lab",
            "identity_value": "stable",
            "connection_target": "stable-target",
            "os_version": "15",
            "device_type": "real_device",
            "connection_type": "network",
            "ip_address": "10.0.0.4",
        },
        connection_behavior={"allow_transport_identity_until_host_resolution": True},
    )
    assert normalized_payload["identity_scope"] == "lab"
    assert normalized_payload["os_version"] == "15"
    assert normalized_payload["connection_type"] == ConnectionType.network

    scoped_payload = device_write._resolve_create_payload_fields(
        DeviceVerificationCreate(
            pack_id="pack",
            platform_id="android",
            identity_value="serial",
            connection_target="serial",
            name="scoped",
            host_id=uuid.uuid4(),
        ),
        resolved_identity_scope="host",
    )
    assert scoped_payload["identity_scope"] == "host"


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


async def test_async_payload_pack_lookup_fallback_and_required_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    session = object()
    monkeypatch.setattr(device_write, "resolve_pack_platform", AsyncMock(side_effect=LookupError("missing")))

    with pytest.raises(ValueError, match="pack_id"):
        await device_write.prepare_device_create_payload_async(
            session,  # type: ignore[arg-type]
            DeviceVerificationCreate(
                pack_id="",
                platform_id="android",
                identity_scope="host",
                identity_value="serial",
                connection_target="serial",
                name="missing pack",
                host_id=uuid.uuid4(),
            ),
        )
    with pytest.raises(ValueError, match="platform_id"):
        await device_write.prepare_device_create_payload_async(
            session,  # type: ignore[arg-type]
            DeviceVerificationCreate(
                pack_id="pack",
                platform_id="",
                identity_scope="host",
                identity_value="serial",
                connection_target="serial",
                name="missing platform",
                host_id=uuid.uuid4(),
            ),
        )

    payload = await device_write.prepare_device_create_payload_async(
        session,  # type: ignore[arg-type]
        DeviceVerificationCreate(
            pack_id="pack",
            platform_id="android",
            identity_scope="host",
            identity_value="serial",
            connection_target="serial",
            name="fallback",
            host_id=uuid.uuid4(),
        ),
    )
    assert payload["identity_scope"] == "host"

    device = SimpleNamespace(
        pack_id="pack",
        platform_id="android",
        identity_scheme="serial",
        identity_scope="host",
        identity_value="serial",
        connection_target="serial",
        name="Device",
        os_version="14",
        host_id=uuid.uuid4(),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        ip_address=None,
        device_config={"canonical_identity": "old"},
    )
    update_payload = await device_write.prepare_device_update_payload_async(
        session,  # type: ignore[arg-type]
        device,  # type: ignore[arg-type]
        DeviceVerificationUpdate(name="updated", host_id=device.host_id),
    )
    assert update_payload["device_config"] == {}
