import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import ConnectionType, Device, DeviceAvailabilityStatus, DeviceType
from app.models.host import Host, HostStatus, OSType
from app.services import device_service
from app.services.node_manager import NodeManagerError, RemoteNodeManager, get_node_manager
from app.services.node_manager_remote import (
    build_agent_start_payload,
    restart_node_via_agent,
    start_remote_temporary_node,
)
from app.services.node_manager_types import TemporaryNodeHandle
from tests.helpers import create_device_record, create_host

HOST_PAYLOAD = {
    "hostname": "remote-host",
    "ip": "192.168.1.50",
    "os_type": "linux",
    "agent_port": 5100,
}

DEVICE_PAYLOAD = {
    "identity_value": "remote-dev-001",
    "connection_target": "remote-dev-001",
    "name": "Remote Android",
    "pack_id": "appium-uiautomator2",
    "platform_id": "android_mobile",
    "identity_scheme": "android_serial",
    "identity_scope": "host",
    "os_version": "14",
}

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _mock_agent_response(json_data: dict[str, Any], status_code: int = 200) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


async def test_remote_start_node(client: AsyncClient, db_session: AsyncSession) -> None:
    host = await create_host(client, **HOST_PAYLOAD)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value=DEVICE_PAYLOAD["identity_value"],
        connection_target=DEVICE_PAYLOAD["connection_target"],
        name=DEVICE_PAYLOAD["name"],
        pack_id=DEVICE_PAYLOAD["pack_id"],
        platform_id=DEVICE_PAYLOAD["platform_id"],
        identity_scheme=DEVICE_PAYLOAD["identity_scheme"],
        identity_scope=DEVICE_PAYLOAD["identity_scope"],
        os_version=DEVICE_PAYLOAD["os_version"],
    )

    mock_client = AsyncMock()
    mock_client.post.return_value = _mock_agent_response(
        {"pid": 9876, "port": 4723, "connection_target": "remote-dev-001"}
    )
    mock_client.get.return_value = _mock_agent_response({"running": True, "port": 4723})
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.services.node_manager_remote.assert_runnable", new=AsyncMock(return_value=None)),
        patch("app.services.node_manager.httpx.AsyncClient", return_value=mock_client),
    ):
        resp = await client.post(f"/api/devices/{device.id}/node/start")

    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert data["state"] == NodeState.running.value
    assert data["pid"] == 9876

    device_resp = await client.get(f"/api/devices/{device.id}")
    assert device_resp.json()["availability_status"] == DeviceAvailabilityStatus.available.value


async def test_remote_start_node_attaches_node_to_device_instance(db_session: AsyncSession) -> None:
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
        availability_status=DeviceAvailabilityStatus.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()
    loaded_device = await device_service.get_device(db_session, device.id)
    assert loaded_device is not None

    mock_client = AsyncMock()
    mock_client.post.return_value = _mock_agent_response(
        {"pid": 9876, "port": 4723, "connection_target": "remote-dev-attach"}
    )
    mock_client.get.return_value = _mock_agent_response({"running": True, "port": 4723})
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.services.node_manager_remote.assert_runnable", new=AsyncMock(return_value=None)),
        patch("app.services.node_manager.httpx.AsyncClient", return_value=mock_client),
    ):
        node = await RemoteNodeManager().start_node(db_session, loaded_device)

    assert loaded_device.appium_node is node
    assert node.state == NodeState.running
    assert loaded_device.availability_status == DeviceAvailabilityStatus.available


async def test_remote_stop_node(client: AsyncClient, db_session: AsyncSession) -> None:
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
        identity_value="remote-dev-001",
        connection_target="remote-dev-001",
        name="Remote Android",
        os_version="14",
        host_id=host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", pid=9876, state=NodeState.running)
    db_session.add(node)
    await db_session.commit()

    # Stop
    mock_stop_client = AsyncMock()
    mock_stop_client.post.return_value = _mock_agent_response({"stopped": True, "port": 4723})
    mock_stop_client.__aenter__ = AsyncMock(return_value=mock_stop_client)
    mock_stop_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(
            "app.services.node_manager.RemoteNodeManager._agent_url",
            new=AsyncMock(return_value="http://192.168.1.50:5100"),
        ),
        patch("app.services.node_manager.httpx.AsyncClient", return_value=mock_stop_client),
    ):
        resp = await client.post(f"/api/devices/{device.id}/node/stop")

    assert resp.status_code == 200, resp.json()
    assert resp.json()["state"] == NodeState.stopped.value

    device_resp = await client.get(f"/api/devices/{device.id}")
    assert device_resp.json()["availability_status"] == DeviceAvailabilityStatus.offline.value


async def test_mark_node_started_acquires_device_row_lock(db_session: AsyncSession) -> None:
    from app.services import node_manager_state

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
        availability_status=DeviceAvailabilityStatus.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()
    loaded = await device_service.get_device(db_session, device.id)
    assert loaded is not None

    real = node_manager_state._hold_device_row_lock
    spy = AsyncMock(side_effect=real)
    with patch("app.services.node_manager_state._hold_device_row_lock", spy):
        await node_manager_state.mark_node_started(db_session, loaded, port=4723, pid=12345)

    spy.assert_awaited_once()
    assert spy.await_args.args[1] == loaded.id


async def test_mark_node_started_raises_when_device_already_deleted(db_session: AsyncSession) -> None:
    from sqlalchemy import delete as sa_delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services import node_manager_state
    from app.services.event_bus import event_bus
    from app.services.node_manager_types import NodeManagerError

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
        availability_status=DeviceAvailabilityStatus.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()
    loaded = await device_service.get_device(db_session, device.id)
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
        await node_manager_state.mark_node_started(db_session, loaded, port=4723, pid=12345)

    publish_spy.assert_not_awaited()


async def test_mark_node_stopped_acquires_device_row_lock(db_session: AsyncSession) -> None:
    from app.services import node_manager_state

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
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", pid=9876, state=NodeState.running)
    db_session.add(node)
    device.appium_node = node
    await db_session.commit()
    loaded = await device_service.get_device(db_session, device.id)
    assert loaded is not None

    real = node_manager_state._hold_device_row_lock
    spy = AsyncMock(side_effect=real)
    with patch("app.services.node_manager_state._hold_device_row_lock", spy):
        await node_manager_state.mark_node_stopped(db_session, loaded)

    spy.assert_awaited_once()
    assert spy.await_args.args[1] == loaded.id


@pytest.mark.parametrize(
    "availability_status",
    [DeviceAvailabilityStatus.busy, DeviceAvailabilityStatus.reserved],
)
async def test_mark_node_stopped_preserves_claimed_availability(
    db_session: AsyncSession,
    availability_status: DeviceAvailabilityStatus,
) -> None:
    from app.services import node_manager_state

    host = Host(
        hostname=f"claim-host-{availability_status.value}",
        ip="192.168.1.54",
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
        identity_value=f"claim-stop-{availability_status.value}",
        connection_target=f"claim-stop-{availability_status.value}",
        name=f"Claim Stop {availability_status.value}",
        os_version="14",
        host_id=host.id,
        availability_status=availability_status,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", pid=9876, state=NodeState.running)
    db_session.add(node)
    device.appium_node = node
    await db_session.commit()
    loaded = await device_service.get_device(db_session, device.id)
    assert loaded is not None

    await node_manager_state.mark_node_stopped(db_session, loaded)

    assert loaded.availability_status == availability_status


async def test_restart_node_via_agent_skips_db_running_old_port_and_starts_next_candidate(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="restart-port-conflict-001",
        connection_target="restart-port-conflict-001",
        name="Restart Port Conflict",
        availability_status="available",
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        pid=123,
        state=NodeState.running,
    )
    db_session.add(node)
    await db_session.commit()

    loaded = await device_service.get_device(db_session, device.id)
    assert loaded is not None
    assert loaded.appium_node is not None

    with (
        patch("app.services.node_manager_remote.stop_remote_temporary_node", new_callable=AsyncMock),
        patch("app.services.node_manager_remote.start_remote_temporary_node", new_callable=AsyncMock) as start_mock,
    ):
        start_mock.return_value = TemporaryNodeHandle(
            port=4724,
            pid=456,
            active_connection_target="restart-port-conflict-001",
        )

        restarted = await restart_node_via_agent(
            db_session,
            loaded,
            loaded.appium_node,
            http_client_factory=AsyncMock,
        )

    assert restarted is True
    await db_session.refresh(loaded.appium_node)
    assert loaded.appium_node.port == 4724
    assert loaded.appium_node.pid == 456
    assert loaded.appium_node.state == NodeState.running
    start_mock.assert_awaited_once()
    assert start_mock.await_args.kwargs["port"] == 4724


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
        availability_status=DeviceAvailabilityStatus.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )

    manager = get_node_manager(device)
    assert isinstance(manager, RemoteNodeManager)

    with pytest.raises(NodeManagerError, match="has no host assigned"):
        await manager._agent_url(device)


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

    with patch("app.services.node_manager_remote.settings_service") as mock_settings:
        mock_settings.get.side_effect = lambda key: "http://grid:4444" if key == "grid.hub_url" else True
        payload = build_agent_start_payload(device, 4723)

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

    with patch("app.services.node_manager_remote.settings_service") as mock_settings:
        mock_settings.get.side_effect = lambda key: "http://grid:4444" if key == "grid.hub_url" else True
        payload = build_agent_start_payload(device, 4724)

    assert payload["headless"] is False


@pytest.mark.asyncio
async def test_build_payload_stereotype_caps_do_not_include_browser_name_for_android_mobile(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Chrome session routing is now handled by the agent emitting dual TOML relay
    slots (one for native apps, one for Chrome).  The backend stereotype_caps must
    NOT inject browserName so that native-app sessions can also be routed."""
    host = await create_host(client, **HOST_PAYLOAD)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="stereotype-android-001",
        connection_target="emulator-5554",
        name="Android Browser Device",
    )

    with patch("app.services.node_manager_remote.settings_service") as mock_settings:
        mock_settings.get.side_effect = lambda key: "http://grid:4444" if key == "grid.hub_url" else True
        payload = build_agent_start_payload(device, 4725)

    assert payload["extra_caps"] is None
    # browserName is intentionally absent from stereotype_caps — the agent adds
    # a second relay slot for Chrome when building the Grid node TOML.
    assert "browserName" not in (payload["stereotype_caps"] or {})
    assert payload["stereotype_caps"]["appium:gridfleet:deviceId"] == str(device.id)
    assert payload["stereotype_caps"]["appium:platform"] == device.platform_id


@pytest.mark.asyncio
async def test_start_remote_temporary_node_aligns_simulator_caps_with_probe_request(
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
    loaded = await device_service.get_device(db_session, device.id)
    assert loaded is not None

    start_response = _mock_agent_response(
        {"pid": 24680, "port": 4724, "connection_target": "315C5A92-07A9-45D2-8210-6B7FB88B406E"}
    )

    with (
        patch("app.services.node_manager_remote.assert_runnable", new=AsyncMock(return_value=None)),
        patch(
            "app.services.node_manager_remote.appium_start", new=AsyncMock(return_value=start_response)
        ) as start_mock,
        patch("app.services.node_manager_remote.appium_status", new=AsyncMock(return_value={"running": True})),
        patch(
            "app.services.node_manager_remote.render_stereotype",
            new=AsyncMock(return_value={"appium:automationName": "XCUITest"}),
        ),
        patch("app.services.node_manager_remote.get_default_plugins", return_value=[]),
        patch("app.services.node_manager_remote.settings_service") as mock_settings,
    ):
        mock_settings.get.side_effect = lambda key: {
            "grid.hub_url": "http://selenium-hub:4444",
            "appium.session_override": True,
            "appium.startup_timeout_sec": 30,
        }[key]
        await start_remote_temporary_node(
            db_session,
            loaded,
            port=4724,
            allocated_caps={"appium:wdaLocalPort": 8100},
            agent_base="http://192.168.88.105:5100",
            http_client_factory=AsyncMock(),
        )

    assert start_mock.await_args is not None
    payload = start_mock.await_args.kwargs["payload"]
    assert payload["extra_caps"]["appium:automationName"] == "XCUITest"
    assert "appium:platformVersion" not in payload["extra_caps"]
    assert "appium:simulatorRunning" not in payload["extra_caps"]


async def test_start_remote_temporary_node_rejects_disabled_pack(client: AsyncClient, db_session: AsyncSession) -> None:
    from sqlalchemy import select

    from app.errors import PackDisabledError
    from app.models.driver_pack import DriverPack
    from tests.pack.factories import seed_test_packs

    await seed_test_packs(db_session)
    host = await create_host(client, **HOST_PAYLOAD)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value=DEVICE_PAYLOAD["identity_value"],
        connection_target=DEVICE_PAYLOAD["connection_target"],
        name=DEVICE_PAYLOAD["name"],
        pack_id=DEVICE_PAYLOAD["pack_id"],
        platform_id=DEVICE_PAYLOAD["platform_id"],
        identity_scheme=DEVICE_PAYLOAD["identity_scheme"],
        identity_scope=DEVICE_PAYLOAD["identity_scope"],
        os_version=DEVICE_PAYLOAD["os_version"],
    )
    pack = await db_session.scalar(select(DriverPack).where(DriverPack.id == "appium-uiautomator2"))
    pack.state = "disabled"
    await db_session.commit()
    await db_session.refresh(device)

    mock_client_obj = AsyncMock()
    mock_client_obj.__aenter__ = AsyncMock(return_value=mock_client_obj)
    mock_client_obj.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.services.node_manager.httpx.AsyncClient", return_value=mock_client_obj),
        pytest.raises(PackDisabledError),
    ):
        await start_remote_temporary_node(
            db_session,
            device,
            port=4723,
            allocated_caps=None,
            agent_base=f"http://{HOST_PAYLOAD['ip']}:{HOST_PAYLOAD['agent_port']}",
            http_client_factory=lambda: mock_client_obj,
        )

    mock_client_obj.post.assert_not_called()


async def test_start_remote_temporary_node_renders_stereotype_once(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import node_manager_remote, pack_capability_service, pack_start_shim
    from tests.pack.factories import seed_test_packs

    await seed_test_packs(db_session)
    host = await create_host(client, **HOST_PAYLOAD)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value=DEVICE_PAYLOAD["identity_value"],
        connection_target=DEVICE_PAYLOAD["connection_target"],
        name=DEVICE_PAYLOAD["name"],
        pack_id=DEVICE_PAYLOAD["pack_id"],
        platform_id=DEVICE_PAYLOAD["platform_id"],
        identity_scheme=DEVICE_PAYLOAD["identity_scheme"],
        identity_scope=DEVICE_PAYLOAD["identity_scope"],
        os_version=DEVICE_PAYLOAD["os_version"],
    )
    loaded = await device_service.get_device(db_session, device.id)
    assert loaded is not None

    calls = 0
    original = pack_capability_service.render_stereotype

    async def counting(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)

    # Patch the locally-bound name in both consumer modules so we count every call
    monkeypatch.setattr(node_manager_remote, "render_stereotype", counting)
    monkeypatch.setattr(pack_start_shim, "render_stereotype", counting)

    mock_client_obj = AsyncMock()
    mock_client_obj.post.return_value = _mock_agent_response(
        {"pid": 9876, "port": 4723, "connection_target": DEVICE_PAYLOAD["connection_target"]}
    )
    mock_client_obj.get.return_value = _mock_agent_response({"running": True, "port": 4723})
    mock_client_obj.__aenter__ = AsyncMock(return_value=mock_client_obj)
    mock_client_obj.__aexit__ = AsyncMock(return_value=False)

    def _client_factory(**_kwargs: object) -> AsyncMock:
        return mock_client_obj

    with patch("app.services.node_manager.httpx.AsyncClient", return_value=mock_client_obj):
        await start_remote_temporary_node(
            db_session,
            loaded,
            port=4723,
            allocated_caps=None,
            agent_base=f"http://{HOST_PAYLOAD['ip']}:{HOST_PAYLOAD['agent_port']}",
            http_client_factory=_client_factory,
        )

    assert calls == 1
