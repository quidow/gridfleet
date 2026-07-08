import uuid
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from app.appium_nodes.services.reconciler_agent import build_agent_start_payload
from app.devices.models import ConnectionType, Device, DeviceType
from app.packs.services.capability import render_stereotype
from app.packs.services.start_shim import PackStartPayloadError, build_pack_start_payload
from tests.fakes import FakeSettingsReader
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def _android_real_device() -> MagicMock:
    """Minimal Device-like mock carrying the attributes the start payload builder reads."""
    device: MagicMock = MagicMock()
    device.id = "00000000-0000-0000-0000-000000000099"
    device.pack_id = "appium-uiautomator2"
    device.platform_id = "android_mobile"
    device.device_type = DeviceType.real_device
    device.connection_type = MagicMock(value="usb")
    device.ip_address = None
    device.name = "gate-pixel"
    device.model = "Pixel 6"
    device.manufacturer = "Google"
    device.os_version = "14"
    device.tags = {}
    return device


@pytest.mark.asyncio
async def test_uiautomator2_stereotype_uses_device_template(
    db_session: AsyncSession,
    _android_real_device: MagicMock,
) -> None:
    """Pack manifest stereotype interpolates {device.*} placeholders."""
    await seed_test_packs(db_session)
    await db_session.commit()

    stereotype = await render_stereotype(
        db_session,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        device_context={
            "platform_id": _android_real_device.platform_id,
            "os_version": _android_real_device.os_version,
            "device_type": _android_real_device.device_type.value,
        },
    )
    assert stereotype["platformName"] == "Android"
    assert stereotype["appium:automationName"] == "UiAutomator2"
    assert stereotype["appium:platform"] == "android_mobile"
    assert stereotype["appium:os_version"] == _android_real_device.os_version
    assert stereotype["appium:device_type"] == "real_device"
    # Redundant appium:platformName mirror is removed in favor of unprefixed platformName.
    assert "appium:platformName" not in stereotype


async def test_start_payload_sends_manifest_appium_platform_name(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="SERIAL1",
        connection_target="SERIAL1",
        name="Pixel",
        os_version="14",
        host_id=uuid.uuid4(),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )

    payload = build_agent_start_payload(device, 4723, settings=FakeSettingsReader({}))
    stereotype = await render_stereotype(
        db_session,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
    )
    pack_payload = await build_pack_start_payload(db_session, device=device, stereotype=stereotype)

    assert pack_payload is not None
    payload.update(
        {
            "pack_id": pack_payload["pack_id"],
            "platform_id": pack_payload["platform_id"],
            "appium_platform_name": pack_payload["appium_platform_name"],
        }
    )

    assert payload["appium_platform_name"] == "Android"
    assert payload["platform_id"] == "android_mobile"
    assert "platform_name" not in payload


@pytest.mark.asyncio
async def test_pack_owned_device_missing_catalog_raises(db_session: AsyncSession) -> None:
    device = Device(
        pack_id="missing-pack",
        platform_id="missing-platform",
        identity_scheme="vendor_serial",
        identity_scope="host",
        identity_value="SERIAL1",
        connection_target="SERIAL1",
        name="Missing Catalog Device",
        os_version="1",
        host_id=uuid.uuid4(),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )

    with pytest.raises(PackStartPayloadError, match="missing-pack:missing-platform"):
        await build_pack_start_payload(db_session, device=device)
