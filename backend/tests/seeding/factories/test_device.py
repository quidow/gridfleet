from __future__ import annotations

import uuid

from app.devices.models import ConnectionType, DeviceOperationalState, DeviceType
from app.seeding.factories.device import _PACK_IDENTITY_BY_PLATFORM_ID, make_device
from tests.seeding.helpers import build_test_seed_context


def test_make_real_android_device() -> None:
    ctx = build_test_seed_context(seed=42)
    host_id = uuid.uuid4()
    device = make_device(
        ctx,
        host_id=host_id,
        platform_id="android_mobile",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        identity_value="R3AL4ND5XYZ",
        name="Pixel 7",
        model="Pixel 7",
        manufacturer="Google",
        os_version="14",
    )
    assert device.platform_id == "android_mobile"
    assert device.identity_scheme == "android_serial"
    assert device.connection_type is ConnectionType.usb
    assert device.manufacturer == "Google"
    assert device.model == "Pixel 7"
    assert device.verified_at == ctx.now
    assert device.operational_state is DeviceOperationalState.available


def test_make_emulator_uses_virtual_connection_type() -> None:
    ctx = build_test_seed_context(seed=1)
    device = make_device(
        ctx,
        host_id=uuid.uuid4(),
        platform_id="android_mobile",
        device_type=DeviceType.emulator,
        connection_type=ConnectionType.virtual,
        identity_value="emulator-5554",
        name="Pixel 6 emu",
        model="Pixel 6",
        manufacturer="Google",
        os_version="14",
    )
    assert device.connection_type is ConnectionType.virtual
    assert device.identity_scheme == "android_serial"


def test_make_ios_device() -> None:
    ctx = build_test_seed_context(seed=1)
    device = make_device(
        ctx,
        host_id=uuid.uuid4(),
        platform_id="ios",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        identity_value="00008120-001E1D3C0C18002E",
        name="iPhone 14 Pro",
        model="iPhone 14 Pro",
        manufacturer="Apple",
        os_version="17.4",
    )
    assert device.identity_scheme == "apple_udid"


def test_pending_verification_device_has_no_verified_at() -> None:
    ctx = build_test_seed_context(seed=2)
    device = make_device(
        ctx,
        host_id=uuid.uuid4(),
        platform_id="android_mobile",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        identity_value="PENDING123",
        name="Pending",
        model="Pixel 8",
        manufacturer="Google",
        os_version="14",
        verified=False,
    )
    assert device.verified_at is None


def test_roku_platform_uses_curated_pack_identity() -> None:
    pack_id, identity_scheme, identity_scope = _PACK_IDENTITY_BY_PLATFORM_ID["roku_network"]
    assert pack_id == "appium-roku-dlenroc"
    assert identity_scheme == "roku_serial"
    assert identity_scope == "global"
