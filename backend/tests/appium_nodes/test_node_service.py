import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import reconciler_agent as node_agent
from app.appium_nodes.services.reconciler_agent import (
    ReconcilerAgentService,
    agent_url,
    build_agent_start_payload,
)
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from app.hosts.models import Host, HostStatus, OSType
from app.lifecycle.services.operator_node import OperatorNodeLifecycleService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device_record, create_host
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from httpx2 import AsyncClient

_crud = DeviceCrudService(settings=FakeSettingsReader(), identity=DeviceIdentityConflictService(), publisher=event_bus)

HOST_PAYLOAD = {
    "hostname": "remote-host",
    "ip": "192.168.1.50",
    "os_type": "linux",
    "agent_port": 5100,
}

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_remote_start_node_attaches_node_to_device_instance(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    host = Host(
        hostname="remote-host",
        ip="192.168.1.50",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="remote-dev-attach",
        connection_target="remote-dev-attach",
        name="Remote Android",
        os_version="14",
        host_id=host.id,
        host=host,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()
    loaded_device = await _crud.get_device(db_session, device.id)
    assert loaded_device is not None

    with patch("app.appium_nodes.services.reconciler_agent.assert_runnable", new=AsyncMock(return_value=None)):
        resp = await client.post(f"/api/devices/{loaded_device.id}/node/start")

    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert data["desired_state"] == AppiumDesiredState.running.value
    assert data["pid"] is None
    await db_session.refresh(loaded_device, attribute_names=["appium_node"])
    assert loaded_device.appium_node is not None
    assert not loaded_device.appium_node.observed_running
    assert loaded_device.appium_node.desired_state == AppiumDesiredState.running
    assert loaded_device.operational_state == DeviceOperationalState.offline


async def test_start_node_with_verification_caller_skips_readiness(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verification-start-unready",
        connection_target="verification-start-unready",
        name="Verification Start Unready",
        operational_state="offline",
        mark_verified=False,
    )
    await db_session.refresh(device, attribute_names=["appium_node"])

    async def fake_ready(_db: AsyncSession, _device: Device) -> bool:
        return False

    monkeypatch.setattr("app.appium_nodes.services.reconciler_agent.is_ready_for_use_async", fake_ready)
    _svc_settings = FakeSettingsReader({})
    svc = ReconcilerAgentService(
        settings=_svc_settings,
        operator=OperatorNodeLifecycleService(
            review=build_review_service(), settings=_svc_settings, publisher=event_bus
        ),
    )
    node = await svc.start_node(db_session, device, caller="verification")
    assert node.desired_state is AppiumDesiredState.running


async def test_mark_node_started_acquires_device_row_lock(db_session: AsyncSession) -> None:
    from app.appium_nodes.services import reconciler_agent as node_service

    host = Host(
        hostname="lock-host",
        ip="192.168.1.51",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="lock-mark-started",
        connection_target="lock-mark-started",
        name="Lock Started",
        os_version="14",
        host_id=host.id,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()
    loaded = await _crud.get_device(db_session, device.id)
    assert loaded is not None

    real = node_service._hold_device_row_lock
    spy = AsyncMock(side_effect=real)
    with patch("app.appium_nodes.services.reconciler_agent._hold_device_row_lock", spy):
        await node_agent.mark_node_started(
            db_session, loaded, port=4723, pid=12345, settings=FakeSettingsReader({}), publisher=Mock()
        )

    spy.assert_awaited_once()
    assert spy.await_args.args[1] == loaded.id


async def test_mark_node_started_raises_when_device_already_deleted(db_session: AsyncSession) -> None:
    from sqlalchemy import delete as sa_delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.appium_nodes.exceptions import NodeManagerError
    from tests.helpers import test_event_bus as event_bus

    host = Host(
        hostname="lock-host-3",
        ip="192.168.1.53",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="lock-deleted-mid-flight",
        connection_target="lock-deleted-mid-flight",
        name="Deleted Mid Flight",
        os_version="14",
        host_id=host.id,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()
    loaded = await _crud.get_device(db_session, device.id)
    assert loaded is not None
    deleted_id = loaded.id

    # Simulate concurrent delete in another transaction.
    other_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    async with other_factory() as other_db:
        await other_db.execute(sa_delete(Device).where(Device.id == deleted_id))
        await other_db.commit()

    publish_spy = AsyncMock()
    with (
        patch.object(event_bus, "publish", publish_spy),
        pytest.raises(NodeManagerError, match="no longer exists"),
    ):
        await node_agent.mark_node_started(
            db_session, loaded, port=4723, pid=12345, settings=FakeSettingsReader({}), publisher=Mock()
        )

    publish_spy.assert_not_awaited()


async def test_mark_node_stopped_acquires_device_row_lock(db_session: AsyncSession) -> None:
    from app.appium_nodes.services import reconciler_agent as node_service

    host = Host(
        hostname="lock-host-2",
        ip="192.168.1.52",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="lock-mark-stopped",
        connection_target="lock-mark-stopped",
        name="Lock Stopped",
        os_version="14",
        host_id=host.id,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=9876,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        active_connection_target="",
    )
    db_session.add(node)
    device.appium_node = node
    await db_session.commit()
    loaded = await _crud.get_device(db_session, device.id)
    assert loaded is not None

    real = node_service._hold_device_row_lock
    spy = AsyncMock(side_effect=real)
    with patch("app.appium_nodes.services.reconciler_agent._hold_device_row_lock", spy):
        await node_agent.mark_node_stopped(db_session, loaded, publisher=Mock())

    spy.assert_awaited_once()
    assert spy.await_args.args[1] == loaded.id


async def test_legacy_hostless_device_fails_fast_for_remote_management() -> None:
    """Legacy hostless devices should not silently fall back to local management."""
    device = Device(
        id=uuid.uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="local-dev-001",
        connection_target="local-dev-001",
        name="Local Android",
        os_version="14",
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )

    with pytest.raises(NodeManagerError, match="has no host assigned"):
        await agent_url(device)


# ---------------------------------------------------------------------------
# Phase 95: build_agent_start_payload headless flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_payload_headless_defaults_to_true(client: AsyncClient, db_session: AsyncSession) -> None:
    """No emulator_headless tag → headless=True in the payload."""
    host = await create_host(client, **HOST_PAYLOAD)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="avd:Pixel_6",
        connection_target="Pixel_6",
        name="Pixel 6 Emulator",
        device_type="emulator",
    )

    payload = build_agent_start_payload(
        device,
        4723,
        settings=FakeSettingsReader({"appium.session_override": True}),
    )

    assert payload["headless"] is True


@pytest.mark.asyncio
async def test_build_payload_headless_false_when_tag_set(client: AsyncClient, db_session: AsyncSession) -> None:
    """emulator_headless='false' tag → headless=False in the payload."""
    host = await create_host(client, **HOST_PAYLOAD)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="avd:Pixel_9",
        connection_target="Pixel_9",
        name="Pixel 9 Emulator",
        device_type="emulator",
        tags={"emulator_headless": "false"},
    )

    payload = build_agent_start_payload(
        device,
        4724,
        settings=FakeSettingsReader({"appium.session_override": True}),
    )

    assert payload["headless"] is False


# ---------------------------------------------------------------------------
# Restored after push-path deletion (f7c5d947): these guarded behavior of
# build_node_launch_payload, which survives as the shared payload builder for
# the pull channel (app/appium_nodes/routers/agent_state.py). Originally
# exercised through the now-deleted start_remote_node/push flow; rewritten to
# call build_node_launch_payload directly.
# ---------------------------------------------------------------------------


async def test_build_node_launch_payload_aligns_simulator_caps_with_probe_request(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    host = await create_host(
        client,
        hostname="mac-host",
        ip="192.168.88.105",
        os_type="macos",
        agent_port=5100,
    )
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="315C5A92-07A9-45D2-8210-6B7FB88B406E",
        connection_target="315C5A92-07A9-45D2-8210-6B7FB88B406E",
        name="iPhone 17 Simulator",
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="simulator_udid",
        identity_scope="host",
        os_version="18.0",
        device_type="simulator",
    )
    loaded = await _crud.get_device(db_session, device.id)
    assert loaded is not None

    with (
        patch("app.appium_nodes.services.reconciler_agent.assert_runnable", new=AsyncMock(return_value=None)),
        patch(
            "app.appium_nodes.services.reconciler_agent.render_stereotype",
            new=AsyncMock(return_value={"appium:automationName": "XCUITest"}),
        ),
    ):
        payload = await node_agent.build_node_launch_payload(
            db_session,
            loaded,
            port=4724,
            allocated_caps={"appium:wdaLocalPort": 8100},
            settings=FakeSettingsReader(
                {
                    "appium.session_override": True,
                    "appium.startup_timeout_sec": 30,
                }
            ),
        )

    assert payload["extra_caps"]["appium:automationName"] == "XCUITest"
    assert "appium:platformVersion" not in payload["extra_caps"]
    assert "appium:simulatorRunning" not in payload["extra_caps"]


async def test_build_node_launch_payload_renders_stereotype_once(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.packs.services import capability as pack_capability_service
    from app.packs.services import start_shim as pack_start_shim

    host = await create_host(client, **HOST_PAYLOAD)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="remote-dev-001",
        connection_target="remote-dev-001",
        name="Remote Android",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        os_version="14",
    )
    loaded = await _crud.get_device(db_session, device.id)
    assert loaded is not None

    calls = 0
    original = pack_capability_service.render_stereotype

    async def counting(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)

    # Patch the locally-bound name in both consumer modules so we count every call.
    monkeypatch.setattr(node_agent, "render_stereotype", counting)
    monkeypatch.setattr(pack_start_shim, "render_stereotype", counting)

    await node_agent.build_node_launch_payload(
        db_session,
        loaded,
        port=4723,
        allocated_caps=None,
        settings=FakeSettingsReader({}),
    )

    assert calls == 1


async def test_mark_node_started_updates_node_row(db_session: AsyncSession, db_host: Host) -> None:
    from app.devices.services import health as device_health

    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="mark-started-sync-001",
        connection_target="mark-started-sync-001",
        name="Mark Started Sync",
        operational_state="available",
    )
    await db_session.commit()
    loaded = await _crud.get_device(db_session, device.id)
    assert loaded is not None

    await node_agent.mark_node_started(
        db_session, loaded, port=4725, pid=999, settings=FakeSettingsReader({}), publisher=Mock()
    )

    await db_session.refresh(loaded, attribute_names=["appium_node"])
    assert loaded.appium_node is not None
    assert loaded.appium_node.observed_running
    assert device_health.build_public_summary(loaded)["node"]["status"] == "ok"
