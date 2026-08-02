import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.devices.models import ConnectionType, Device, DeviceType
from app.devices.schemas.device import DevicePatch, DeviceVerificationCreate, DeviceVerificationUpdate
from app.devices.services import write as device_write

_PLATFORM_DEFAULT_CASES = [
    # connection_behavior, device_type, connection_type, want
    ({"default_device_type": "emulator"}, None, None, (DeviceType.emulator, ConnectionType.virtual)),
    (
        {"_allowed_device_types": ["emulator"]},
        DeviceType.real_device,
        ConnectionType.usb,
        (DeviceType.emulator, ConnectionType.usb),
    ),
    (
        {"default_connection_type": "network"},
        DeviceType.real_device,
        None,
        (DeviceType.real_device, ConnectionType.network),
    ),
    (
        {"default_device_type": "emulator", "_allowed_device_types": ["emulator"]},
        DeviceType.real_device,
        ConnectionType.usb,
        (DeviceType.emulator, ConnectionType.usb),
    ),
    (
        {"_allowed_connection_types": ["usb"]},
        DeviceType.real_device,
        ConnectionType.network,
        (DeviceType.real_device, ConnectionType.usb),
    ),
]


@pytest.mark.parametrize(
    ("behavior", "device_type", "connection_type", "want"),
    _PLATFORM_DEFAULT_CASES,
    ids=[
        "default-device-type-emulator",
        "single-allowed-device-type-overrides-request",
        "default-connection-type-network",
        "default-and-allowed-device-type-combine",
        "single-allowed-connection-type-overrides-request",
    ],
)
def test_platform_defaults_resolves_type_and_connection(
    behavior: dict[str, object],
    device_type: DeviceType | None,
    connection_type: ConnectionType | None,
    want: tuple[DeviceType, ConnectionType],
) -> None:
    got = device_write._platform_defaults(
        platform_id="p",
        device_type=device_type,
        connection_type=connection_type,
        connection_behavior=behavior,
    )
    assert got == want


_PLATFORM_DEFAULT_REJECTIONS = [
    ({"_allowed_device_types": ["emulator", "simulator"]}, DeviceType.real_device, ConnectionType.usb, "Device type"),
    ({"_allowed_connection_types": ["usb"]}, DeviceType.real_device, ConnectionType.virtual, "Virtual connection"),
    (
        {"_allowed_connection_types": ["usb", "virtual"]},
        DeviceType.real_device,
        ConnectionType.network,
        "Connection type",
    ),
]


@pytest.mark.parametrize(
    ("behavior", "device_type", "connection_type", "message"),
    _PLATFORM_DEFAULT_REJECTIONS,
    ids=[
        "device-type-not-allowed",
        "virtual-connection-not-allowed-for-real-device",
        "connection-type-not-allowed",
    ],
)
def test_platform_defaults_rejects_disallowed_combinations(
    behavior: dict[str, object],
    device_type: DeviceType,
    connection_type: ConnectionType,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        device_write._platform_defaults(
            platform_id="p",
            device_type=device_type,
            connection_type=connection_type,
            connection_behavior=behavior,
        )


_VALIDATE_SHAPE_REJECTIONS = [
    # device_type, connection_type, identity_value, connection_target, ip_address, host_id, connection_behavior, message
    (DeviceType.real_device, ConnectionType.usb, "serial", "serial", None, None, None, "Assigned host"),
    (DeviceType.real_device, ConnectionType.network, "stable", "10.0.0.1:5555", None, uuid.uuid4(), None, "IP address"),
    (
        DeviceType.real_device,
        ConnectionType.usb,
        "stable",
        "stable",
        None,
        uuid.uuid4(),
        {"requires_ip_address": True},
        "IP address",
    ),
    (DeviceType.real_device, ConnectionType.usb, "serial", None, None, uuid.uuid4(), None, "Connection target"),
    (DeviceType.emulator, ConnectionType.usb, "avd", "avd", None, uuid.uuid4(), None, "Emulators"),
    (DeviceType.real_device, ConnectionType.usb, None, "serial", None, uuid.uuid4(), None, "Identity value"),
    (
        DeviceType.real_device,
        ConnectionType.network,
        "10.0.0.1:5555",
        "10.0.0.1:5555",
        "10.0.0.1",
        uuid.uuid4(),
        None,
        "stable identity",
    ),
]


@pytest.mark.parametrize(
    (
        "device_type",
        "connection_type",
        "identity_value",
        "connection_target",
        "ip_address",
        "host_id",
        "connection_behavior",
        "message",
    ),
    _VALIDATE_SHAPE_REJECTIONS,
    ids=[
        "missing-host-id",
        "network-requires-ip-address",
        "behavior-requires-ip-address-regardless-of-connection-type",
        "missing-connection-target",
        "emulator-requires-virtual-connection",
        "missing-identity-value",
        "identity-value-must-be-stable-not-transport",
    ],
)
def test_validate_device_shape_rejects_invalid_combinations(
    device_type: DeviceType,
    connection_type: ConnectionType,
    identity_value: str | None,
    connection_target: str | None,
    ip_address: str | None,
    host_id: uuid.UUID | None,
    connection_behavior: dict[str, object] | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        device_write._validate_device_shape(
            device_type=device_type,
            connection_type=connection_type,
            identity_value=identity_value,
            connection_target=connection_target,
            ip_address=ip_address,
            host_id=host_id,
            connection_behavior=connection_behavior,
        )


def test_validate_device_shape_allows_relaxed_target_and_ip_requirements() -> None:
    device_write._validate_device_shape(
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        identity_value=None,
        connection_target=None,
        ip_address=None,
        host_id=uuid.uuid4(),
        connection_behavior={"requires_connection_target": False, "requires_ip_address": False},
    )


def test_device_config_helpers_and_create_payload_field_resolution() -> None:
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

    explicit = device_write._resolve_identity(
        platform_id="android",
        identity_scheme="serial",
        identity_value="stable",
        connection_target=None,
        ip_address=None,
    )
    assert explicit[0] == "serial"
    assert explicit[1] == "stable"
    assert explicit[2] == "stable"  # connection_target falls back to identity_value
    assert explicit[3] is None  # ip_address

    generated = device_write._resolve_identity(
        platform_id="android",
        identity_scheme=None,
        identity_value=None,
        connection_target=None,
        ip_address=None,
    )
    assert generated[0] == "manager_generated"
    assert generated[1].startswith("android:")
    assert generated[2] == ""  # connection_target when all inputs are None
    assert generated[3] is None  # ip_address

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

    os_payload = device_write._resolve_create_payload_fields(
        DeviceVerificationCreate(
            pack_id="pack",
            platform_id="android",
            identity_scope="host",
            identity_value="serial",
            connection_target="serial",
            name="with-os",
            host_id=uuid.uuid4(),
            os_version="15",
        ),
    )
    assert os_payload["os_version"] == "15"

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
    assert scoped_payload["identity_value"] == "serial"  # identity_scope propagation doesn't clobber identity fields


_RESOLVE_CREATE_PAYLOAD_FIELD_REJECTIONS = [
    # pack_id, platform_id, identity_scope, message
    ("pack", "", "host", "platform_id"),
    ("", "android", "host", "pack_id"),
    ("pack", "android", None, "identity_scope"),
]


@pytest.mark.parametrize(
    ("pack_id", "platform_id", "identity_scope", "message"),
    _RESOLVE_CREATE_PAYLOAD_FIELD_REJECTIONS,
    ids=["missing-platform-id", "missing-pack-id", "missing-identity-scope"],
)
def test_resolve_create_payload_fields_rejects_missing_required_fields(
    pack_id: str,
    platform_id: str,
    identity_scope: str | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        device_write._resolve_create_payload_fields(
            DeviceVerificationCreate(
                pack_id=pack_id,
                platform_id=platform_id,
                identity_scope=identity_scope,
                identity_value="serial",
                connection_target="serial",
                name="name",
                host_id=uuid.uuid4(),
            )
        )


def test_patch_contract_and_update_payload_normalization() -> None:
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
