import asyncio
import uuid
from collections.abc import Callable, Coroutine
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import ConnectionType, Device, DeviceAvailabilityStatus, DeviceType
from app.models.device_event import DeviceEvent, DeviceEventType
from app.models.host import Host, HostStatus, OSType
from app.services import control_plane_state_store, device_health
from app.services.agent_probe_result import ProbeResult
from app.services.heartbeat import (
    APPIUM_RESTART_SEQUENCE_NAMESPACE,
    _auto_sync_plugins_on_recovery,
    _check_hosts,
    _schedule_background_task,
    shutdown_background_tasks,
)
from app.services.host_diagnostics import APPIUM_PROCESSES_NAMESPACE
from app.services.node_health import _check_nodes


async def set_node_health_failure_count(db_session: AsyncSession, node_key: str, count: int) -> None:
    node = await db_session.get(AppiumNode, uuid.UUID(node_key))
    assert node is not None
    node.consecutive_health_failures = count
    await db_session.commit()


async def get_node_health_control_plane_state(db_session: AsyncSession) -> dict[str, int]:
    nodes = (await db_session.execute(select(AppiumNode))).scalars().all()
    return {str(node.id): node.consecutive_health_failures for node in nodes if node.consecutive_health_failures > 0}


async def test_heartbeat_marks_online(db_session: AsyncSession) -> None:
    host = Host(hostname="test-host", ip="10.0.0.1", os_type=OSType.linux, agent_port=5100, status=HostStatus.offline)
    db_session.add(host)
    await db_session.commit()

    with (
        patch(
            "app.services.heartbeat._ping_agent",
            return_value={"status": "ok", "hostname": "test-host", "os_type": "linux", "version": "0.1.0"},
        ),
        patch(
            "app.services.heartbeat._schedule_background_task",
        ),
    ):
        await _check_hosts(db_session)

    await db_session.refresh(host)
    assert host.status == HostStatus.online
    assert host.last_heartbeat is not None


async def test_heartbeat_updates_missing_prerequisites(db_session: AsyncSession) -> None:
    host = Host(
        hostname="prereq-host",
        ip="10.0.0.5",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
        capabilities={"missing_prerequisites": ["java"]},
    )
    db_session.add(host)
    await db_session.commit()

    with patch(
        "app.services.heartbeat._ping_agent",
        return_value={
            "status": "ok",
            "hostname": "prereq-host",
            "os_type": "linux",
            "version": "0.1.0",
            "missing_prerequisites": [],
        },
    ):
        await _check_hosts(db_session)

    await db_session.refresh(host)
    assert host.missing_prerequisites == []
    assert host.capabilities == {"missing_prerequisites": []}


async def test_heartbeat_marks_offline_after_failures(db_session: AsyncSession) -> None:
    host = Host(hostname="test-host", ip="10.0.0.1", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()  # generate host.id before referencing it
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-hb",
        connection_target="dev-hb",
        name="HB Device",
        os_version="14",
        host_id=host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    with patch("app.services.heartbeat._ping_agent", return_value=None):
        await _check_hosts(db_session)  # failure 1
        await _check_hosts(db_session)  # failure 2
        await _check_hosts(db_session)  # failure 3

    await db_session.refresh(host)
    await db_session.refresh(device)
    assert host.status == HostStatus.offline
    assert device.availability_status == DeviceAvailabilityStatus.offline


async def test_host_offline_cascade_publishes_canonical_availability_event(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict[str, object]]] = []

    async def fake_publish(name: str, payload: dict[str, object]) -> None:
        captured.append((name, payload))

    monkeypatch.setattr("app.services.event_bus.event_bus.publish", fake_publish)

    host = Host(
        hostname="cascade-host",
        ip="10.0.0.42",
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
        identity_value="dev-cascade",
        connection_target="dev-cascade",
        name="Cascade Device",
        os_version="14",
        host_id=host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    with patch("app.services.heartbeat._ping_agent", return_value=None):
        await _check_hosts(db_session)
        await _check_hosts(db_session)
        await _check_hosts(db_session)

    availability_events = [payload for name, payload in captured if name == "device.availability_changed"]
    cascade_events = [
        e
        for e in availability_events
        if e.get("device_id") == str(device.id) and e.get("new_availability_status") == "offline"
    ]
    assert len(cascade_events) == 1, (
        f"Expected exactly one cascade event for device, got {len(cascade_events)}: {cascade_events}"
    )
    payload = cascade_events[0]
    assert payload["old_availability_status"] == "available"
    assert payload["new_availability_status"] == "offline"
    assert payload["reason"] == f"Host {host.hostname} offline"


async def test_heartbeat_recovery(db_session: AsyncSession) -> None:
    host = Host(hostname="test-host", ip="10.0.0.1", os_type=OSType.linux, agent_port=5100, status=HostStatus.offline)
    db_session.add(host)
    await db_session.commit()

    # Simulate previous failures
    await control_plane_state_store.set_value(db_session, "heartbeat.failure_count", str(host.id), 5)
    await db_session.commit()

    with (
        patch(
            "app.services.heartbeat._ping_agent",
            return_value={"status": "ok", "hostname": "test-host", "os_type": "linux", "version": "0.1.0"},
        ),
        patch(
            "app.services.heartbeat._schedule_background_task",
        ),
    ):
        await _check_hosts(db_session)

    await db_session.refresh(host)
    assert host.status == HostStatus.online
    assert await control_plane_state_store.get_value(db_session, "heartbeat.failure_count", str(host.id)) is None


async def test_heartbeat_recovery_schedules_driver_sync(db_session: AsyncSession) -> None:
    host = Host(hostname="test-host", ip="10.0.0.1", os_type=OSType.linux, agent_port=5100, status=HostStatus.offline)
    db_session.add(host)
    await db_session.commit()

    scheduled: list[tuple[Callable[..., Coroutine[object, object, None]], tuple[object, ...]]] = []

    def capture_task(task_fn: Callable[..., Coroutine[object, object, None]], *args: object) -> None:
        scheduled.append((task_fn, args))

    with (
        patch(
            "app.services.heartbeat._ping_agent",
            return_value={"status": "ok", "hostname": "test-host", "os_type": "linux", "version": "0.1.0"},
        ),
        patch("app.services.heartbeat._schedule_background_task", side_effect=capture_task),
    ):
        await _check_hosts(db_session)

    assert scheduled == [(_auto_sync_plugins_on_recovery, (host.id,))]


async def test_shutdown_background_tasks_cancels_inflight_recovery_work() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking_task(_: uuid.UUID) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    _schedule_background_task(blocking_task, uuid.uuid4())
    await asyncio.wait_for(started.wait(), 1)

    await shutdown_background_tasks(timeout=0.01)

    assert cancelled.is_set()


async def test_heartbeat_recovery_shutdown_drains_spawned_background_task(db_session: AsyncSession) -> None:
    host = Host(hostname="test-host", ip="10.0.0.1", os_type=OSType.linux, agent_port=5100, status=HostStatus.offline)
    db_session.add(host)
    await db_session.commit()

    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_sync(_: uuid.UUID) -> None:
        started.set()
        await release.wait()

    with (
        patch(
            "app.services.heartbeat._ping_agent",
            return_value={"status": "ok", "hostname": "test-host", "os_type": "linux", "version": "0.1.0"},
        ),
        patch("app.services.heartbeat._auto_sync_plugins_on_recovery", new=blocking_sync),
    ):
        await _check_hosts(db_session)
        await asyncio.wait_for(started.wait(), 1)

        shutdown_task = asyncio.create_task(shutdown_background_tasks(timeout=0.01))
        await asyncio.sleep(0)
        assert not shutdown_task.done()

        release.set()
        await asyncio.wait_for(shutdown_task, 1)


async def test_heartbeat_ingests_agent_restart_events_once_and_updates_control_plane_state(
    db_session: AsyncSession,
) -> None:
    host = Host(hostname="agent-host", ip="10.0.0.1", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-agent-1",
        connection_target="dev-agent-1",
        name="Agent Restart Phone",
        os_version="14",
        host_id=host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", pid=1111, state=NodeState.running)
    db_session.add(node)
    await db_session.commit()

    await set_node_health_failure_count(db_session, str(node.id), 2)
    payload = {
        "status": "ok",
        "hostname": "agent-host",
        "os_type": "linux",
        "version": "0.1.0",
        "appium_processes": {
            "running_nodes": [
                {
                    "port": 4723,
                    "pid": 2222,
                    "connection_target": "dev-agent-1",
                    "platform_id": "android_mobile",
                }
            ],
            "recent_restart_events": [
                {
                    "sequence": 1,
                    "kind": "crash_detected",
                    "port": 4723,
                    "pid": 1111,
                    "attempt": 1,
                    "delay_sec": 1,
                    "exit_code": 1,
                    "occurred_at": "2026-04-04T10:00:00+00:00",
                    "will_retry": True,
                },
                {
                    "sequence": 2,
                    "kind": "restart_succeeded",
                    "port": 4723,
                    "pid": 2222,
                    "attempt": 1,
                    "delay_sec": 1,
                    "occurred_at": "2026-04-04T10:00:01+00:00",
                    "will_retry": False,
                },
            ],
        },
    }

    with (
        patch("app.services.heartbeat._ping_agent", return_value=payload),
        patch("app.services.heartbeat._schedule_background_task"),
    ):
        await _check_hosts(db_session)
        await _check_hosts(db_session)

    await db_session.refresh(node)
    assert node.pid == 2222
    assert node.state == NodeState.running
    assert await control_plane_state_store.get_value(db_session, APPIUM_RESTART_SEQUENCE_NAMESPACE, str(host.id)) == 2
    assert str(node.id) not in await get_node_health_control_plane_state(db_session)
    process_snapshot = await control_plane_state_store.get_value(db_session, APPIUM_PROCESSES_NAMESPACE, str(host.id))
    assert isinstance(process_snapshot, dict)
    assert isinstance(process_snapshot["reported_at"], str)
    assert process_snapshot["running_nodes"] == [
        {
            "port": 4723,
            "pid": 2222,
            "connection_target": "dev-agent-1",
            "platform_id": "android_mobile",
        }
    ]

    events = (
        (
            await db_session.execute(
                select(DeviceEvent).where(DeviceEvent.device_id == device.id).order_by(DeviceEvent.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert [event.event_type for event in events] == [DeviceEventType.node_crash, DeviceEventType.node_restart]
    assert events[0].details is not None
    assert events[0].details["kind"] == "crash_detected"
    assert events[0].details["process"] == "appium"
    assert events[0].details["occurred_at"] == "2026-04-04T10:00:00+00:00"
    assert events[1].details is not None
    assert events[1].details["kind"] == "restart_succeeded"
    assert events[1].details["process"] == "appium"
    assert events[1].details["occurred_at"] == "2026-04-04T10:00:01+00:00"
    await db_session.refresh(node)
    assert node.health_running is None
    assert node.health_state is None
    assert device_health.build_public_summary(device)["healthy"] is True


async def test_restart_exhausted_keeps_backend_fallback_available(db_session: AsyncSession) -> None:
    host = Host(hostname="agent-host", ip="10.0.0.2", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-agent-2",
        connection_target="dev-agent-2",
        name="Fallback Phone",
        os_version="14",
        host_id=host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(device_id=device.id, port=4724, grid_url="http://hub:4444", pid=3333, state=NodeState.running)
    db_session.add(node)
    await db_session.commit()

    exhausted_payload = {
        "status": "ok",
        "hostname": "agent-host",
        "os_type": "linux",
        "version": "0.1.0",
        "appium_processes": {
            "running_nodes": [],
            "recent_restart_events": [
                {
                    "sequence": 10,
                    "kind": "crash_detected",
                    "port": 4724,
                    "pid": 3333,
                    "attempt": 6,
                    "exit_code": 9,
                    "occurred_at": "2026-04-04T10:10:00+00:00",
                    "will_retry": False,
                },
                {
                    "sequence": 11,
                    "kind": "restart_exhausted",
                    "port": 4724,
                    "pid": 3333,
                    "attempt": 6,
                    "exit_code": 9,
                    "occurred_at": "2026-04-04T10:10:01+00:00",
                    "will_retry": False,
                },
            ],
        },
    }

    with (
        patch("app.services.heartbeat._ping_agent", return_value=exhausted_payload),
        patch("app.services.heartbeat._schedule_background_task"),
    ):
        await _check_hosts(db_session)

    await db_session.refresh(node)
    await db_session.refresh(device)
    assert node.state == NodeState.running
    assert device.availability_status == DeviceAvailabilityStatus.available
    process_snapshot = await control_plane_state_store.get_value(db_session, APPIUM_PROCESSES_NAMESPACE, str(host.id))
    assert isinstance(process_snapshot, dict)
    assert process_snapshot["running_nodes"] == []

    events = (
        (
            await db_session.execute(
                select(DeviceEvent).where(DeviceEvent.device_id == device.id).order_by(DeviceEvent.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert [event.details["kind"] for event in events if event.details is not None] == [
        "crash_detected",
        "restart_exhausted",
    ]

    await set_node_health_failure_count(db_session, str(node.id), 2)
    with (
        patch("app.services.node_health._check_node_health", return_value=ProbeResult(status="refused")),
        patch("app.services.node_health._restart_node_via_agent", return_value=True),
    ):
        await _check_nodes(db_session)

    await db_session.refresh(node)
    assert node.state == NodeState.running


async def test_grid_relay_restart_events_degrade_and_restore_health_summary(
    db_session: AsyncSession,
) -> None:
    host = Host(hostname="agent-host", ip="10.0.0.3", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-agent-3",
        connection_target="dev-agent-3",
        name="Relay Phone",
        os_version="14",
        host_id=host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(device_id=device.id, port=4725, grid_url="http://hub:4444", pid=4444, state=NodeState.running)
    db_session.add(node)
    await db_session.commit()

    await set_node_health_failure_count(db_session, str(node.id), 2)
    payload = {
        "status": "ok",
        "hostname": "agent-host",
        "os_type": "linux",
        "version": "0.1.0",
        "appium_processes": {
            "running_nodes": [
                {
                    "port": 4725,
                    "pid": 4444,
                    "connection_target": "dev-agent-3",
                    "platform_id": "android_mobile",
                }
            ],
            "recent_restart_events": [
                {
                    "sequence": 20,
                    "process": "grid_relay",
                    "kind": "crash_detected",
                    "port": 4725,
                    "pid": 7777,
                    "attempt": 1,
                    "delay_sec": 1,
                    "exit_code": 12,
                    "occurred_at": "2026-04-04T10:20:00+00:00",
                    "will_retry": True,
                },
                {
                    "sequence": 21,
                    "process": "grid_relay",
                    "kind": "restart_succeeded",
                    "port": 4725,
                    "pid": 8888,
                    "attempt": 1,
                    "delay_sec": 1,
                    "occurred_at": "2026-04-04T10:20:01+00:00",
                    "will_retry": False,
                },
            ],
        },
    }

    with (
        patch("app.services.heartbeat._ping_agent", return_value=payload),
        patch("app.services.heartbeat._schedule_background_task"),
    ):
        await _check_hosts(db_session)

    await db_session.refresh(node)
    assert node.pid == 4444
    assert node.state == NodeState.running
    assert str(node.id) not in await get_node_health_control_plane_state(db_session)

    events = (
        (
            await db_session.execute(
                select(DeviceEvent).where(DeviceEvent.device_id == device.id).order_by(DeviceEvent.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert [event.event_type for event in events] == [DeviceEventType.node_crash, DeviceEventType.node_restart]
    assert events[0].details is not None
    assert events[0].details["process"] == "grid_relay"
    assert events[1].details is not None
    assert events[1].details["process"] == "grid_relay"

    await db_session.refresh(node)
    assert node.health_running is None
    assert node.health_state is None
    assert device_health.build_public_summary(device)["healthy"] is True


async def test_grid_relay_restart_exhausted_sets_relay_specific_degraded_state(
    db_session: AsyncSession,
) -> None:
    host = Host(hostname="agent-host", ip="10.0.0.4", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-agent-4",
        connection_target="dev-agent-4",
        name="Relay Exhausted Phone",
        os_version="14",
        host_id=host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(device_id=device.id, port=4726, grid_url="http://hub:4444", pid=5555, state=NodeState.running)
    db_session.add(node)
    await db_session.commit()

    payload = {
        "status": "ok",
        "hostname": "agent-host",
        "os_type": "linux",
        "version": "0.1.0",
        "appium_processes": {
            "running_nodes": [],
            "recent_restart_events": [
                {
                    "sequence": 30,
                    "process": "grid_relay",
                    "kind": "crash_detected",
                    "port": 4726,
                    "pid": 9999,
                    "attempt": 6,
                    "exit_code": 15,
                    "occurred_at": "2026-04-04T10:30:00+00:00",
                    "will_retry": False,
                },
                {
                    "sequence": 31,
                    "process": "grid_relay",
                    "kind": "restart_exhausted",
                    "port": 4726,
                    "pid": 9999,
                    "attempt": 6,
                    "exit_code": 15,
                    "occurred_at": "2026-04-04T10:30:01+00:00",
                    "will_retry": False,
                },
            ],
        },
    }

    with (
        patch("app.services.heartbeat._ping_agent", return_value=payload),
        patch("app.services.heartbeat._schedule_background_task"),
    ):
        await _check_hosts(db_session)

    await db_session.refresh(node)
    assert node.health_running is False
    assert node.health_state == "relay_restart_exhausted"
