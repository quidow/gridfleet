import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumDesiredState
from app.appium_nodes.services import reconciler_agent as node_agent
from app.appium_nodes.services.reconciler_agent import (
    NodeStartDetails,
    ReconcilerAgentService,
    require_management_host,
)
from app.core.timeutil import now_utc
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
    from sqlalchemy.ext.asyncio import AsyncSession

_crud = DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus)

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
    assert loaded_device.operational_state_last_emitted == DeviceOperationalState.offline


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
    from app.devices import locking as device_locking

    locked = await device_locking.lock_device_handle(db_session, device.id)
    node = await svc.start_node_txn(db_session, locked, caller="verification")
    assert node.desired_state is AppiumDesiredState.running


async def test_mark_node_helpers_take_no_lock_of_their_own(db_session: AsyncSession) -> None:
    """The aggregate-lock contract: the caller locks Device once and passes the
    proof plus the locked child; the helpers must not re-lock either row."""
    from app.appium_nodes.services import locking as appium_node_locking
    from app.devices import locking as device_locking
    from app.devices.services.decision_snapshot import load_device_decision_snapshot

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

    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, now=now_utc())
    locked_node = await appium_node_locking.lock_appium_node_for_device(db_session, device.id)

    lock_device = AsyncMock(side_effect=AssertionError("mark_node_* must not lock Device"))
    lock_device_handle = AsyncMock(side_effect=AssertionError("mark_node_* must not lock Device"))
    lock_node = AsyncMock(side_effect=AssertionError("mark_node_* must not lock AppiumNode"))
    with (
        patch.object(device_locking, "lock_device", lock_device),
        patch.object(device_locking, "lock_device_handle", lock_device_handle),
        patch.object(appium_node_locking, "lock_appium_node_for_device", lock_node),
    ):
        snapshot = await node_agent.mark_node_started(
            db_session,
            locked,
            locked_node,
            snapshot,
            port=4723,
            pid=12345,
            details=NodeStartDetails(),
            settings=FakeSettingsReader({}),
            publisher=Mock(),
        )

    await db_session.refresh(device, attribute_names=["appium_node"])
    assert device.appium_node is not None
    assert device.appium_node.observed_running

    with (
        patch.object(device_locking, "lock_device", lock_device),
        patch.object(device_locking, "lock_device_handle", lock_device_handle),
        patch.object(appium_node_locking, "lock_appium_node_for_device", lock_node),
    ):
        await node_agent.mark_node_stopped(
            db_session,
            locked,
            device.appium_node,
            snapshot,
            publisher=Mock(),
        )

    await db_session.refresh(device.appium_node)
    assert device.appium_node.pid is None
    assert device.appium_node.active_connection_target is None


async def test_mark_node_stopped_returns_the_snapshot_when_the_node_row_is_gone(db_session: AsyncSession) -> None:
    """A node deleted between the caller's inventory and its lock is a no-op, not
    an attribute error on ``None``."""
    from app.devices import locking as device_locking
    from app.devices.services.decision_snapshot import load_device_decision_snapshot

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
    await db_session.commit()

    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, now=now_utc())
    publisher = Mock()

    result = await node_agent.mark_node_stopped(db_session, locked, None, snapshot, publisher=publisher)

    assert result is snapshot
    publisher.queue_for_session.assert_not_called()


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
        require_management_host(device)


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
            settings=FakeSettingsReader({"appium.startup_timeout_sec": 30}),
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
    from app.appium_nodes.services import locking as appium_node_locking
    from app.devices import locking as device_locking
    from app.devices.services import health as device_health
    from app.devices.services.decision_snapshot import load_device_decision_snapshot

    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="mark-started-sync-001",
        connection_target="mark-started-sync-001",
        name="Mark Started Sync",
        operational_state="available",
    )
    await db_session.commit()

    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, now=now_utc())
    locked_node = await appium_node_locking.lock_appium_node_for_device(db_session, device.id)

    await node_agent.mark_node_started(
        db_session,
        locked,
        locked_node,
        snapshot,
        port=4725,
        pid=999,
        details=NodeStartDetails(),
        settings=FakeSettingsReader({}),
        publisher=Mock(),
    )

    loaded = locked.device
    await db_session.refresh(loaded, attribute_names=["appium_node"])
    assert loaded.appium_node is not None
    assert loaded.appium_node.observed_running
    assert device_health.build_public_summary(loaded)["node"]["status"] == "ok"
