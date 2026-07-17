import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.appium_nodes.services import (
    reconciler_agent as appium_reconciler_agent,
)
from app.appium_nodes.services.reconciler_agent import build_agent_start_payload
from app.devices.models import ConnectionType, Device, DeviceType
from app.packs.services.capability import render_stereotype
from app.packs.services.start_shim import PackStartPayloadError, build_pack_start_payload
from tests.fakes import FakeSettingsReader
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class _FakeHost:
    ip = "127.0.0.1"
    agent_port = 5100
    tool_env: dict[str, str] | None = None


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


# ---------------------------------------------------------------------------
# Restored after push-path deletion (f7c5d947): these guarded behavior of
# build_node_launch_payload, which survives as the shared payload builder for
# the pull channel (app/appium_nodes/routers/agent_state.py). Originally
# exercised through the now-deleted start_remote_node/push flow; rewritten to
# call build_node_launch_payload directly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_node_launch_payload_forwards_pack_appium_env(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    device = Device(
        id=uuid.uuid4(),
        pack_id="appium-xcuitest",
        platform_id="tvos",
        identity_scheme="apple_udid",
        identity_scope="global",
        identity_value="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        connection_target="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        name="Living Room",
        os_version="26.4",
        host_id=uuid.uuid4(),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        ip_address="192.168.1.5",
        device_config={"wda_base_url": "http://192.168.1.5"},
    )

    monkeypatch.setattr(appium_reconciler_agent, "require_management_host", lambda device, action: _FakeHost())
    monkeypatch.setattr(appium_reconciler_agent, "_build_session_aligned_start_caps", AsyncMock(return_value=None))
    monkeypatch.setattr(
        appium_reconciler_agent,
        "build_agent_start_payload",
        lambda device, port, **kwargs: {
            "connection_target": device.connection_target,
            "platform_id": device.platform_id,
            "port": port,
            "extra_caps": None,
            "device_type": device.device_type.value,
            "ip_address": device.ip_address,
            "allocated_caps": None,
            "session_override": True,
            "headless": True,
        },
    )

    payload = await appium_reconciler_agent.build_node_launch_payload(
        db_session,
        device,
        port=4723,
        allocated_caps=None,
        settings=FakeSettingsReader({}),
    )

    assert payload["appium_env"] == {"APPIUM_XCUITEST_PREFER_DEVICECTL": "1"}


@pytest.mark.asyncio
async def test_build_node_launch_payload_stamps_pack_release(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launch payload carries the release its caps/lifecycle data came from,
    selected exactly as the agent's desired driver-packs endpoint selects it —
    the agent requires both to agree before starting a node."""
    from sqlalchemy import select

    from app.packs.models import DriverPack
    from app.packs.services.release_ordering import selected_release

    await seed_test_packs(db_session)
    await db_session.commit()

    device = Device(
        id=uuid.uuid4(),
        pack_id="appium-xcuitest",
        platform_id="tvos",
        identity_scheme="apple_udid",
        identity_scope="global",
        identity_value="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        connection_target="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        name="Living Room",
        os_version="26.4",
        host_id=uuid.uuid4(),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        ip_address="192.168.1.5",
        device_config={"wda_base_url": "http://192.168.1.5"},
    )

    monkeypatch.setattr(appium_reconciler_agent, "require_management_host", lambda device, action: _FakeHost())
    monkeypatch.setattr(appium_reconciler_agent, "_build_session_aligned_start_caps", AsyncMock(return_value=None))

    payload = await appium_reconciler_agent.build_node_launch_payload(
        db_session,
        device,
        port=4723,
        allocated_caps=None,
        settings=FakeSettingsReader({}),
    )

    pack = (await db_session.execute(select(DriverPack).where(DriverPack.id == "appium-xcuitest"))).scalar_one()
    await db_session.refresh(pack, attribute_names=["releases"])
    expected = selected_release(pack.releases, pack.current_release)
    assert expected is not None
    assert payload["pack_release"] == expected.release


@pytest.mark.asyncio
async def test_build_node_launch_payload_rejects_mid_build_release_switch(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The payload's release-owned fields are read in separate queries; under
    READ COMMITTED a concurrent release switch can tear them. The build reads
    the selected release at entry and refuses the payload when the stamp
    derived at the end disagrees — the agent skips this tick and the next poll
    derives everything from the new release."""
    from pathlib import Path

    from sqlalchemy import select

    from app.appium_nodes.exceptions import NodeManagerError
    from app.packs.manifest import load_manifest_yaml
    from app.packs.models import DriverPack
    from app.packs.services import start_shim as pack_start_shim
    from tests.packs.factories import seed_manifest_pack

    await seed_test_packs(db_session)
    fixture = Path(__file__).parent / "fixtures" / "manifests" / "appium-xcuitest.yaml"
    second = load_manifest_yaml(fixture.read_text().replace("release: 2026.04.12", "release: 9999.01.1"))
    await seed_manifest_pack(db_session, second)
    pack = (await db_session.execute(select(DriverPack).where(DriverPack.id == "appium-xcuitest"))).scalar_one()
    pack.current_release = "2026.04.12"
    await db_session.commit()

    device = Device(
        id=uuid.uuid4(),
        pack_id="appium-xcuitest",
        platform_id="tvos",
        identity_scheme="apple_udid",
        identity_scope="global",
        identity_value="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        connection_target="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        name="Living Room",
        os_version="26.4",
        host_id=uuid.uuid4(),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        ip_address="192.168.1.5",
        device_config={"wda_base_url": "http://192.168.1.5"},
    )

    real_build = pack_start_shim.build_pack_start_payload

    async def switching_build(session: AsyncSession, **kwargs: object) -> dict[str, object] | None:
        row = (await session.execute(select(DriverPack).where(DriverPack.id == "appium-xcuitest"))).scalar_one()
        row.current_release = "9999.01.1"
        await session.flush()
        return await real_build(session, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(appium_reconciler_agent, "require_management_host", lambda device, action: _FakeHost())
    monkeypatch.setattr(appium_reconciler_agent, "_build_session_aligned_start_caps", AsyncMock(return_value=None))
    monkeypatch.setattr(appium_reconciler_agent, "build_pack_start_payload", switching_build)

    with pytest.raises(NodeManagerError, match="release"):
        await appium_reconciler_agent.build_node_launch_payload(
            db_session,
            device,
            port=4723,
            allocated_caps=None,
            settings=FakeSettingsReader({}),
        )


@pytest.mark.asyncio
async def test_build_node_launch_payload_sends_device_field_caps_only_to_appium_defaults(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    device = Device(
        id=uuid.uuid4(),
        pack_id="appium-roku-dlenroc",
        platform_id="roku_network",
        identity_scheme="roku_serial",
        identity_scope="global",
        identity_value="roku-serial",
        connection_target="192.168.1.2",
        name="Roku Stick",
        os_version="15.1.4",
        ip_address="192.168.1.2",
        host_id=uuid.uuid4(),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        device_config={"roku_password": "dev-password"},
    )

    monkeypatch.setattr(appium_reconciler_agent, "require_management_host", lambda device, action: _FakeHost())

    payload = await appium_reconciler_agent.build_node_launch_payload(
        db_session,
        device,
        port=4724,
        allocated_caps=None,
        settings=FakeSettingsReader({}),
    )

    assert payload["extra_caps"]["appium:password"] == "dev-password"
    assert payload["extra_caps"]["appium:ip"] == "192.168.1.2"
    assert "stereotype_caps" not in payload


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
