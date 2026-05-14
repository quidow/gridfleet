import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AgentCallError
from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.models.host import Host, OSType
from app.services import appium_reconciler_agent as node_agent
from app.services.node_service_types import NodeManagerError, NodePortConflictError, RemoteStartResult
from app.services.pack_start_shim import PackStartPayloadError
from tests.helpers import create_device_record

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


@dataclass
class _FakeResponse:
    payload: dict[str, object]

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


@dataclass
class _ErrorResponse:
    status_code: int
    payload: dict[str, object]

    def raise_for_status(self) -> None:
        request = httpx.Request("POST", "http://agent/start")
        response = httpx.Response(self.status_code, request=request, json=self.payload)
        raise httpx.HTTPStatusError("bad", request=request, response=response)

    def json(self) -> dict[str, object]:
        return self.payload


async def _loaded_device(db_session: AsyncSession, db_host: Host, identity: str) -> Device:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value=identity,
        connection_target=identity,
        name=identity,
        operational_state=DeviceOperationalState.available,
    )
    from app.services import device_service

    loaded = await device_service.get_device(db_session, device.id)
    assert loaded is not None
    return loaded


async def test_mark_node_started_rejects_hostless_device_after_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = Device(
        id=__import__("uuid").uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="mark-start-hostless",
        connection_target="mark-start-hostless",
        name="mark-start-hostless",
        os_version="14",
        host_id=None,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://grid")
    fake_db = MagicMock()
    fake_db.flush = AsyncMock()
    monkeypatch.setattr("app.services.appium_reconciler_agent._hold_device_row_lock", AsyncMock(return_value=device))
    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.appium_node_locking.lock_appium_node_for_device",
        AsyncMock(return_value=node),
    )

    with pytest.raises(NodeManagerError, match="no host assigned"):
        await node_agent.mark_node_started(
            fake_db,
            device,
            port=4723,
            pid=123,
            allocated_caps={"appium:systemPort": 8200, "custom:flag": "yes"},
        )


async def test_start_remote_node_error_and_override_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await _loaded_device(db_session, db_host, "start-remote-branches")
    fake_platform = SimpleNamespace(appium_platform_name="Android")
    monkeypatch.setattr("app.services.appium_reconciler_agent.assert_runnable", AsyncMock())
    monkeypatch.setattr("app.services.appium_reconciler_agent.render_stereotype", AsyncMock(return_value={}))
    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.resolve_pack_platform", AsyncMock(return_value=fake_platform)
    )
    monkeypatch.setattr(
        "app.services.appium_reconciler_agent._build_session_aligned_start_caps", AsyncMock(return_value={})
    )
    monkeypatch.setattr("app.services.appium_reconciler_agent._merge_appium_default_pack_caps", AsyncMock())

    monkeypatch.setattr("app.services.appium_reconciler_agent.resolve_pack_for_device", lambda _device: None)
    with pytest.raises(NodeManagerError, match="no driver pack platform"):
        await node_agent.start_remote_node(
            db_session,
            device,
            port=4723,
            allocated_caps={},
            agent_base="http://agent",
            http_client_factory=httpx.AsyncClient,
        )

    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.resolve_pack_for_device",
        lambda _device: ("appium-uiautomator2", "android_mobile"),
    )
    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.build_pack_start_payload",
        AsyncMock(side_effect=PackStartPayloadError("bad manifest")),
    )
    with pytest.raises(NodeManagerError, match="bad manifest"):
        await node_agent.start_remote_node(
            db_session,
            device,
            port=4723,
            allocated_caps={},
            agent_base="http://agent",
            http_client_factory=httpx.AsyncClient,
        )

    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.build_pack_start_payload",
        AsyncMock(
            return_value={
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "appium_platform_name": "Android",
                "stereotype_caps": {"browserName": "Chrome"},
                "grid_slots": 2,
                "lifecycle_actions": {"health": "check"},
                "connection_behavior": {"default_connection_type": "usb"},
                "insecure_features": ["adb_shell"],
                "workaround_env": {"A": "B"},
            }
        ),
    )
    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.appium_start",
        AsyncMock(return_value=_FakeResponse({"pid": 4321, "connection_target": "live-target"})),
    )

    result = await node_agent.start_remote_node(
        db_session,
        device,
        port=4724,
        allocated_caps={"appium:systemPort": 8201},
        agent_base="http://agent",
        http_client_factory=httpx.AsyncClient,
    )

    assert result == RemoteStartResult(
        port=4724,
        pid=4321,
        active_connection_target="live-target",
        agent_base="http://agent",
    )


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (AgentCallError("10.0.0.1", "agent down"), AgentCallError),
        (httpx.ConnectError("network down"), NodeManagerError),
    ],
)
async def test_start_remote_node_propagates_agent_call_errors(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    exception: Exception,
    expected: type[Exception],
) -> None:
    device = await _loaded_device(db_session, db_host, f"start-error-{expected.__name__}")
    monkeypatch.setattr("app.services.appium_reconciler_agent.assert_runnable", AsyncMock())
    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.resolve_pack_for_device",
        lambda _device: ("appium-uiautomator2", "android_mobile"),
    )
    monkeypatch.setattr("app.services.appium_reconciler_agent.render_stereotype", AsyncMock(return_value={}))
    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.resolve_pack_platform",
        AsyncMock(return_value=SimpleNamespace(appium_platform_name="Android")),
    )
    monkeypatch.setattr(
        "app.services.appium_reconciler_agent._build_session_aligned_start_caps", AsyncMock(return_value={})
    )
    monkeypatch.setattr("app.services.appium_reconciler_agent._merge_appium_default_pack_caps", AsyncMock())
    monkeypatch.setattr("app.services.appium_reconciler_agent.build_pack_start_payload", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.appium_reconciler_agent.appium_start", AsyncMock(side_effect=exception))

    with pytest.raises(expected):
        await node_agent.start_remote_node(
            db_session,
            device,
            port=4723,
            allocated_caps={},
            agent_base="http://agent",
            http_client_factory=httpx.AsyncClient,
        )


async def test_start_remote_node_maps_agent_http_status_errors(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await _loaded_device(db_session, db_host, "start-http-status-branches")
    monkeypatch.setattr("app.services.appium_reconciler_agent.assert_runnable", AsyncMock())
    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.resolve_pack_for_device",
        lambda _device: ("appium-uiautomator2", "android_mobile"),
    )
    monkeypatch.setattr("app.services.appium_reconciler_agent.render_stereotype", AsyncMock(return_value={}))
    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.resolve_pack_platform",
        AsyncMock(return_value=SimpleNamespace(appium_platform_name="Android")),
    )
    monkeypatch.setattr(
        "app.services.appium_reconciler_agent._build_session_aligned_start_caps",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr("app.services.appium_reconciler_agent._merge_appium_default_pack_caps", AsyncMock())
    monkeypatch.setattr("app.services.appium_reconciler_agent.build_pack_start_payload", AsyncMock(return_value=None))

    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.appium_start",
        AsyncMock(return_value=_ErrorResponse(409, {"detail": {"code": "PORT_OCCUPIED", "message": "busy"}})),
    )
    with pytest.raises(NodePortConflictError, match="busy"):
        await node_agent.start_remote_node(
            db_session,
            device,
            port=4723,
            allocated_caps={},
            agent_base="http://agent",
            http_client_factory=httpx.AsyncClient,
        )

    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.appium_start",
        AsyncMock(return_value=_ErrorResponse(500, {"detail": {"message": "agent failed"}})),
    )
    with pytest.raises(NodeManagerError, match="agent failed"):
        await node_agent.start_remote_node(
            db_session,
            device,
            port=4723,
            allocated_caps={},
            agent_base="http://agent",
            http_client_factory=httpx.AsyncClient,
        )


async def test_restart_node_via_agent_covers_retry_and_failure_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    host = Host(
        id=__import__("uuid").uuid4(),
        hostname="restart-host",
        ip="10.0.0.10",
        os_type=OSType.linux,
        agent_port=5100,
    )
    device = Device(
        id=__import__("uuid").uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="restart-branches",
        connection_target="restart-branches",
        name="restart-branches",
        os_version="14",
        host_id=__import__("uuid").uuid4(),
        host=host,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://grid", pid=1, active_connection_target="old")
    fake_db = AsyncMock()

    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.device_locking.lock_device", AsyncMock(return_value=device)
    )
    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.appium_node_locking.lock_appium_node_for_device",
        AsyncMock(return_value=None),
    )
    assert (
        await node_agent.restart_node_via_agent(fake_db, device, node, http_client_factory=httpx.AsyncClient) is False
    )

    hostless = Device(
        id=__import__("uuid").uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="restart-hostless",
        connection_target="restart-hostless",
        name="restart-hostless",
        os_version="14",
        host_id=None,
        host=None,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    assert (
        await node_agent.restart_node_via_agent(fake_db, hostless, node, http_client_factory=httpx.AsyncClient) is False
    )

    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.appium_node_locking.lock_appium_node_for_device",
        AsyncMock(return_value=node),
    )
    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.appium_node_resource_service.get_capabilities", AsyncMock(return_value={})
    )
    monkeypatch.setattr("app.services.appium_reconciler_agent.stop_remote_node", AsyncMock(return_value=False))
    assert (
        await node_agent.restart_node_via_agent(fake_db, device, node, http_client_factory=httpx.AsyncClient) is False
    )

    monkeypatch.setattr("app.services.appium_reconciler_agent.stop_remote_node", AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.appium_reconciler_agent.candidate_ports", AsyncMock(return_value=[4723, 4724]))
    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.start_remote_node",
        AsyncMock(
            side_effect=[
                NodePortConflictError("busy"),
                RemoteStartResult(port=4724, pid=2, active_connection_target="new", agent_base="http://agent"),
            ]
        ),
    )

    assert await node_agent.restart_node_via_agent(fake_db, device, node, http_client_factory=httpx.AsyncClient) is True
    assert node.port == 4724
    assert node.pid == 2
    assert node.active_connection_target == "new"

    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.start_remote_node",
        AsyncMock(side_effect=NodeManagerError("no ports")),
    )
    assert (
        await node_agent.restart_node_via_agent(fake_db, device, node, http_client_factory=httpx.AsyncClient) is False
    )

    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.start_remote_node",
        AsyncMock(side_effect=NodePortConflictError("busy")),
    )
    assert (
        await node_agent.restart_node_via_agent(fake_db, device, node, http_client_factory=httpx.AsyncClient) is False
    )

    monkeypatch.setattr("app.services.appium_reconciler_agent.candidate_ports", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.services.appium_reconciler_agent.start_remote_node", AsyncMock())
    assert (
        await node_agent.restart_node_via_agent(fake_db, device, node, http_client_factory=httpx.AsyncClient) is False
    )

    monkeypatch.setattr("app.services.appium_reconciler_agent.candidate_ports", AsyncMock(return_value=[4723, 4724]))
    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.start_remote_node",
        AsyncMock(side_effect=httpx.ConnectError("agent gone")),
    )
    assert (
        await node_agent.restart_node_via_agent(fake_db, device, node, http_client_factory=httpx.AsyncClient) is False
    )


async def test_start_stop_restart_node_guard_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await _loaded_device(db_session, db_host, "start-stop-guards")
    with pytest.raises(NodeManagerError, match="No running node"):
        await node_agent.stop_node(db_session, device)

    monkeypatch.setattr("app.services.appium_reconciler_agent.is_ready_for_use_async", AsyncMock(return_value=False))
    monkeypatch.setattr(
        "app.services.appium_reconciler_agent.readiness_error_detail_async",
        AsyncMock(return_value="not ready"),
    )
    with pytest.raises(NodeManagerError, match="not ready"):
        await node_agent.start_node(db_session, device)

    monkeypatch.setattr("app.services.appium_reconciler_agent.is_ready_for_use_async", AsyncMock(return_value=True))
    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://grid", pid=1, active_connection_target="active")
    db_session.add(node)
    await db_session.commit()
    device.appium_node = node
    with pytest.raises(NodeManagerError, match="already running"):
        await node_agent.start_node(db_session, device)

    restarted = await node_agent.restart_node(db_session, device)
    assert restarted.transition_token is not None
    assert restarted.transition_deadline is not None


async def test_start_stop_wait_and_manual_recovery_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.refresh = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    device_id = uuid.uuid4()
    device = SimpleNamespace(id=device_id, host_id=uuid.uuid4(), appium_node=None)
    monkeypatch.setattr(node_agent, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(node_agent, "candidate_ports", AsyncMock(return_value=[4723]))
    monkeypatch.setattr(node_agent.settings_service, "get", lambda key: "http://grid")
    write = AsyncMock()
    monkeypatch.setattr(node_agent, "write_desired_state", write)

    node = await node_agent.start_node(db, device)
    assert node.port == 4723
    assert device.appium_node is node
    write.assert_awaited_once()

    running = AppiumNode(device_id=device_id, port=4724, grid_url="http://grid", pid=1, active_connection_target="dev")
    stopped = await node_agent.stop_node(db, SimpleNamespace(id=device_id, appium_node=running))
    assert stopped is running
    assert write.await_args.kwargs["target"] == AppiumDesiredState.stopped

    node_id = uuid.uuid4()
    not_running = AppiumNode(device_id=device_id, port=4725, grid_url="http://grid")
    running_node = AppiumNode(
        device_id=device_id, port=4725, grid_url="http://grid", pid=2, active_connection_target="dev"
    )
    db.refresh.reset_mock()
    db.get = AsyncMock(side_effect=[not_running, running_node])
    monkeypatch.setattr(node_agent.asyncio, "sleep", AsyncMock())
    found = await node_agent.wait_for_node_running(db, node_id, timeout_sec=1, poll_interval_sec=0)
    assert found is running_node
    assert db.get.await_count == 2
    assert db.refresh.await_count == 2

    db.get = AsyncMock(return_value=None)
    assert await node_agent.wait_for_node_running(db, node_id, timeout_sec=0, poll_interval_sec=0) is None

    clean_device = SimpleNamespace(id=device_id, lifecycle_policy_state={})
    monkeypatch.setattr(node_agent, "_hold_device_row_lock", AsyncMock(return_value=clean_device))
    await node_agent._clear_manual_recovery_suppression(db, device_id)

    dirty_device = SimpleNamespace(id=device_id, lifecycle_policy_state={"last_failure_reason": "boom"})
    monkeypatch.setattr(node_agent, "_hold_device_row_lock", AsyncMock(return_value=dirty_device))
    monkeypatch.setattr(node_agent, "record_manual_recovered", MagicMock())
    monkeypatch.setattr(node_agent, "write_lifecycle_policy_state", MagicMock())
    await node_agent._clear_manual_recovery_suppression(db, device_id)
    node_agent.record_manual_recovered.assert_called_once()


async def test_mark_node_started_records_non_port_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    device = Device(
        id=uuid.uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="mark-start-caps",
        connection_target="mark-start-caps",
        name="mark-start-caps",
        os_version="14",
        host_id=uuid.uuid4(),
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    monkeypatch.setattr(node_agent.settings_service, "get", lambda key: "http://grid")
    monkeypatch.setattr(node_agent, "_hold_device_row_lock", AsyncMock(return_value=device))
    monkeypatch.setattr(
        node_agent.appium_node_locking,
        "lock_appium_node_for_device",
        AsyncMock(return_value=None),
    )
    set_extra = AsyncMock()
    monkeypatch.setattr(node_agent.appium_node_resource_service, "set_node_extra_capability", set_extra)
    monkeypatch.setattr(node_agent, "ready_operational_state", AsyncMock(return_value=DeviceOperationalState.available))
    monkeypatch.setattr(node_agent, "set_operational_state", AsyncMock())
    monkeypatch.setattr(node_agent.device_health, "apply_node_state_transition", AsyncMock())

    node = await node_agent.mark_node_started(
        db,
        device,
        port=4723,
        pid=123,
        allocated_caps={"appium:systemPort": 8200, "custom:flag": "yes"},
    )

    assert node is device.appium_node
    set_extra.assert_awaited_once_with(db, node_id=node.id, capability_key="custom:flag", value="yes")


async def test_mark_node_started_clears_stale_reconciler_failure(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await _loaded_device(db_session, db_host, "mark-start-clear")
    device.lifecycle_policy_state = {
        "last_failure_source": "appium_reconciler",
        "last_failure_reason": "http_error",
    }
    await db_session.commit()

    monkeypatch.setattr(node_agent.settings_service, "get", lambda key: "http://grid")
    monkeypatch.setattr(node_agent, "_hold_device_row_lock", AsyncMock(return_value=device))
    monkeypatch.setattr(
        node_agent.appium_node_locking,
        "lock_appium_node_for_device",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(node_agent.appium_node_resource_service, "set_node_extra_capability", AsyncMock())
    monkeypatch.setattr(node_agent, "ready_operational_state", AsyncMock(return_value=DeviceOperationalState.available))
    monkeypatch.setattr(node_agent, "set_operational_state", AsyncMock())
    monkeypatch.setattr(node_agent.device_health, "apply_node_state_transition", AsyncMock())

    await node_agent.mark_node_started(
        db_session,
        device,
        port=4723,
        pid=123,
    )

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state.get("last_failure_source") is None
    assert reloaded.lifecycle_policy_state.get("last_failure_reason") is None


async def test_pack_cap_helpers_cover_empty_and_stereotype_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    device = SimpleNamespace(
        pack_id="pack",
        platform_id="android",
        device_type=SimpleNamespace(value="real_device"),
        ip_address="10.0.0.5",
        connection_target="serial",
        identity_value="serial",
        os_version="14",
        device_config={"udid": "serial"},
    )
    monkeypatch.setattr(node_agent, "resolve_pack_for_device", lambda _device: None)
    assert await node_agent._build_appium_default_pack_caps(AsyncMock(), device) == {}

    monkeypatch.setattr(node_agent, "build_appium_driver_caps", lambda _device, **_kwargs: {"platformName": "Android"})
    monkeypatch.setattr(node_agent.appium_capability_keys, "manager_owned_cap_keys", lambda _keys: frozenset())
    monkeypatch.setattr(node_agent, "resolve_pack_for_device", lambda _device: ("pack", "android"))
    monkeypatch.setattr(
        node_agent,
        "render_stereotype",
        AsyncMock(return_value={"appium:automationName": "UiAutomator2"}),
    )
    caps = await node_agent._build_session_aligned_start_caps(AsyncMock(), device, allocated_caps={})

    assert caps["appium:automationName"] == "UiAutomator2"


async def test_stop_node_via_agent_handles_host_and_http_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    host = SimpleNamespace(ip="10.0.0.5", agent_port=5100)
    device = SimpleNamespace(host=host)
    node = SimpleNamespace(port=4723)

    monkeypatch.setattr(node_agent, "require_management_host", MagicMock(side_effect=NodeManagerError("missing host")))
    assert await node_agent.stop_node_via_agent(device, node, http_client_factory=httpx.AsyncClient) is False

    response = MagicMock()
    response.raise_for_status.return_value = None
    monkeypatch.setattr(node_agent, "require_management_host", MagicMock(return_value=host))
    monkeypatch.setattr(node_agent, "appium_stop", AsyncMock(return_value=response))
    assert await node_agent.stop_node_via_agent(device, node, http_client_factory=httpx.AsyncClient) is True

    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "bad",
        request=httpx.Request("POST", "http://agent"),
        response=httpx.Response(500, request=httpx.Request("POST", "http://agent")),
    )
    assert await node_agent.stop_node_via_agent(device, node, http_client_factory=httpx.AsyncClient) is False


async def test_start_and_restart_guard_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.refresh = AsyncMock()
    hostless = SimpleNamespace(id=uuid.uuid4(), host_id=None, appium_node=None)
    monkeypatch.setattr(node_agent, "is_ready_for_use_async", AsyncMock(return_value=True))
    with pytest.raises(NodeManagerError, match="has no host assigned"):
        await node_agent.start_node(db, hostless)

    start = AsyncMock(return_value="started")
    monkeypatch.setattr(node_agent, "start_node", start)
    result = await node_agent.restart_node(db, SimpleNamespace(appium_node=None))
    assert result == "started"
    start.assert_awaited_once()


async def test_start_for_node_reserves_resources_and_derived_data(monkeypatch: pytest.MonkeyPatch) -> None:
    device = SimpleNamespace(
        id=uuid.uuid4(),
        host_id=uuid.uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        device_type=SimpleNamespace(value="real_device"),
    )
    node = SimpleNamespace(id=uuid.uuid4())
    reserve_session = AsyncMock()
    reserve_session.commit = AsyncMock()

    class SessionFactory:
        def __call__(self) -> "SessionFactory":
            return self

        async def __aenter__(self) -> AsyncMock:
            return reserve_session

        async def __aexit__(self, *_args: object) -> None:
            return None

    fake_platform = SimpleNamespace(
        parallel_resources=SimpleNamespace(
            ports=[SimpleNamespace(capability_name="appium:systemPort", start=8200)],
            derived_data_path=True,
        )
    )
    monkeypatch.setattr(node_agent, "_short_session_factory", lambda _db: SessionFactory())
    monkeypatch.setattr(node_agent, "resolve_pack_platform", AsyncMock(return_value=fake_platform))
    monkeypatch.setattr(
        node_agent.appium_node_resource_service,
        "get_capabilities",
        AsyncMock(return_value={"appium:systemPort": 9000}),
    )
    reserve = AsyncMock()
    monkeypatch.setattr(node_agent.appium_node_resource_service, "reserve", reserve)
    monkeypatch.setattr(node_agent, "agent_url", AsyncMock(return_value="http://agent"))
    monkeypatch.setattr(node_agent, "candidate_ports", AsyncMock(return_value=[4723]))
    monkeypatch.setattr(node_agent, "reserve_appium_port", AsyncMock())
    monkeypatch.setattr(
        node_agent,
        "start_remote_node",
        AsyncMock(
            return_value=RemoteStartResult(port=4723, pid=1, active_connection_target="dev", agent_base="http://agent")
        ),
    )

    handle = await node_agent._start_for_node(AsyncMock(), device, node=node)

    assert handle.allocated_caps["appium:systemPort"] == 9000
    assert handle.allocated_caps["appium:derivedDataPath"].startswith("/tmp/gridfleet/derived-data/")
    reserve.assert_not_awaited()


async def test_start_for_node_hostless_and_resource_reservation_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    hostless = SimpleNamespace(
        id=uuid.uuid4(),
        host_id=None,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        device_type=SimpleNamespace(value="real_device"),
    )
    node = SimpleNamespace(id=uuid.uuid4())
    with pytest.raises(NodeManagerError, match="has no host assigned"):
        await node_agent._start_for_node(AsyncMock(), hostless, node=node)

    device = SimpleNamespace(
        id=uuid.uuid4(),
        host_id=uuid.uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        device_type=SimpleNamespace(value="real_device"),
    )
    reserve_session = AsyncMock()
    reserve_session.commit = AsyncMock()

    class SessionFactory:
        def __call__(self) -> "SessionFactory":
            return self

        async def __aenter__(self) -> AsyncMock:
            return reserve_session

        async def __aexit__(self, *_args: object) -> None:
            return None

    fake_platform = SimpleNamespace(
        parallel_resources=SimpleNamespace(
            ports=[SimpleNamespace(capability_name="appium:systemPort", start=8200)],
            derived_data_path=False,
        )
    )
    release_managed = AsyncMock()
    monkeypatch.setattr(node_agent, "_short_session_factory", lambda _db: SessionFactory())
    monkeypatch.setattr(node_agent, "resolve_pack_platform", AsyncMock(return_value=fake_platform))
    monkeypatch.setattr(node_agent.appium_node_resource_service, "get_capabilities", AsyncMock(return_value={}))
    monkeypatch.setattr(node_agent.appium_node_resource_service, "reserve", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(node_agent.appium_node_resource_service, "release_managed", release_managed)

    with pytest.raises(RuntimeError, match="boom"):
        await node_agent._start_for_node(AsyncMock(), device, node=node)

    release_managed.assert_awaited_once()
    assert reserve_session.commit.await_count == 1


async def test_start_for_node_cleans_up_after_all_port_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    device = SimpleNamespace(
        id=uuid.uuid4(),
        host_id=uuid.uuid4(),
        pack_id="missing-pack",
        platform_id="missing",
        device_type=None,
    )
    node = SimpleNamespace(id=uuid.uuid4())
    cleanup_session = AsyncMock()
    cleanup_session.commit = AsyncMock()

    class SessionFactory:
        def __call__(self) -> "SessionFactory":
            return self

        async def __aenter__(self) -> AsyncMock:
            return cleanup_session

        async def __aexit__(self, *_args: object) -> None:
            return None

    release_managed = AsyncMock()
    monkeypatch.setattr(node_agent, "_short_session_factory", lambda _db: SessionFactory())
    monkeypatch.setattr(node_agent, "resolve_pack_platform", AsyncMock(side_effect=LookupError))
    monkeypatch.setattr(node_agent.appium_node_resource_service, "get_capabilities", AsyncMock(return_value={}))
    monkeypatch.setattr(node_agent, "agent_url", AsyncMock(return_value="http://agent"))
    monkeypatch.setattr(node_agent, "candidate_ports", AsyncMock(return_value=[4723, 4724]))
    monkeypatch.setattr(node_agent, "reserve_appium_port", AsyncMock())
    monkeypatch.setattr(node_agent.appium_node_resource_service, "release_capability", AsyncMock())
    monkeypatch.setattr(node_agent.appium_node_resource_service, "release_managed", release_managed)
    monkeypatch.setattr(node_agent, "start_remote_node", AsyncMock(side_effect=NodePortConflictError("busy")))

    with pytest.raises(NodePortConflictError):
        await node_agent._start_for_node(AsyncMock(), device, node=node)

    assert node_agent.appium_node_resource_service.release_capability.await_count == 2
    release_managed.assert_awaited_once()
