import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
import structlog.testing
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.heartbeat import (
    APPIUM_RESTART_SEQUENCE_NAMESPACE,
    HeartbeatService,
)
from app.appium_nodes.services.heartbeat_outcomes import ClientMode, HeartbeatOutcome, HeartbeatPingResult
from app.appium_nodes.services.node_health import NodeHealthService
from app.core.leader import state_store as control_plane_state_store
from app.core.metrics_recorders import HEARTBEAT_PING_TOTAL
from app.core.timeutil import now_utc
from app.devices.models import ConnectionType, Device, DeviceEvent, DeviceEventType, DeviceOperationalState, DeviceType
from app.devices.services import health as device_health
from app.devices.services.health import DeviceHealthService
from app.devices.services.state import derive_operational_state
from app.hosts.models import Host, HostStatus, OSType
from app.hosts.service_status_push import HOST_STATUS_NAMESPACE
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _hb_svc(
    db: AsyncSession,
    *,
    settings: object = None,
    publisher: object = None,
    circuit_breaker: object = None,
) -> HeartbeatService:
    """Create a HeartbeatService wired to the test DB session."""
    factory = AsyncMock()
    factory.__aenter__ = AsyncMock(return_value=db)
    factory.__aexit__ = AsyncMock(return_value=None)

    return HeartbeatService(
        publisher=publisher or Mock(),
        settings=settings or FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=circuit_breaker or Mock(),
        session_factory=lambda: factory,
    )


async def _seed_snapshot(db: AsyncSession, host: Host, appium_processes: dict[str, Any]) -> None:
    """Store a consolidated status-push snapshot the way the push handler would."""
    await control_plane_state_store.set_value(
        db,
        HOST_STATUS_NAMESPACE,
        str(host.id),
        {"received_at": now_utc().isoformat(), "payload": {"appium_processes": appium_processes}},
    )
    await db.commit()


def _unguarded_guard(svc: HeartbeatService) -> object:
    """Advance past the first (always-guarded) cycle and return an unguarded guard."""
    svc.begin_cycle()
    return svc.begin_cycle()


async def set_node_health_failure_count(db_session: AsyncSession, node_key: str, count: int) -> None:
    node = await db_session.get(AppiumNode, uuid.UUID(node_key))
    assert node is not None
    node.consecutive_health_failures = count
    await db_session.commit()


async def get_node_health_control_plane_state(db_session: AsyncSession) -> dict[str, int]:
    nodes = (await db_session.execute(select(AppiumNode))).scalars().all()
    return {str(node.id): node.consecutive_health_failures for node in nodes if node.consecutive_health_failures > 0}


@pytest.fixture(autouse=True)
async def _skip_leader_fencing(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    """No-op the leader fence and redirect per-host sessions to the test schema engine."""
    yield


# ─────────────────────────── liveness (recency verdict) ───────────────────────────


async def test_recent_push_keeps_host_online(db_session: AsyncSession) -> None:
    host = Host(hostname="recent-host", ip="10.0.0.1", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    host.last_heartbeat = now_utc() - timedelta(seconds=10)
    db_session.add(host)
    await db_session.commit()

    svc = _hb_svc(db_session)
    guard = _unguarded_guard(svc)  # second cycle: unguarded
    evaluation = await svc.evaluate_host(db_session, host, guard=guard)

    assert evaluation.alive is True
    assert host.status == HostStatus.online


async def test_stale_push_marks_host_offline_and_cascades_devices(db_session: AsyncSession) -> None:
    """Agent-dies case: last push 10 minutes ago -> offline flip + device cascade."""
    host = Host(hostname="stale-host", ip="10.0.0.2", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    host.last_heartbeat = now_utc() - timedelta(minutes=10)
    db_session.add(host)
    await db_session.flush()
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-stale",
        connection_target="dev-stale",
        name="Stale Device",
        os_version="14",
        host_id=host.id,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    publisher = Mock()
    svc = _hb_svc(db_session, publisher=publisher)
    guard = _unguarded_guard(svc)  # unguarded
    evaluation = await svc.evaluate_host(db_session, host, guard=guard)
    await db_session.commit()

    assert evaluation.alive is False
    assert host.status == HostStatus.offline
    await db_session.refresh(device)
    assert await derive_operational_state(db_session, device, now=now_utc()) is DeviceOperationalState.offline

    events = {call.args[1]: call.args[2] for call in publisher.queue_for_session.call_args_list}
    assert events["host.status_changed"]["new_status"] == "offline"
    lost = events["host.heartbeat_lost"]
    assert "missed_count" not in lost
    assert lost["stale_for_sec"] >= 45
    assert lost["last_push_at"] is not None


async def test_first_cycle_after_boot_never_flips_offline(db_session: AsyncSession) -> None:
    """Backend-restart case: guard is active on the very first begin_cycle()."""
    host = Host(hostname="boot-host", ip="10.0.0.3", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    host.last_heartbeat = now_utc() - timedelta(minutes=10)
    db_session.add(host)
    await db_session.commit()

    svc = _hb_svc(db_session)
    guard = svc.begin_cycle()  # first cycle -> guard.active
    evaluation = await svc.evaluate_host(db_session, host, guard=guard)

    assert evaluation.alive is False
    assert host.status == HostStatus.online  # swallowed


async def test_never_pushed_host_grace_from_created_at(db_session: AsyncSession) -> None:
    """Fresh auto-accepted host: last_heartbeat is None -> measured from created_at."""
    fresh = Host(hostname="fresh-host", ip="10.0.0.4", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(fresh)
    await db_session.commit()
    assert fresh.last_heartbeat is None

    svc = _hb_svc(db_session)
    guard = _unguarded_guard(svc)
    evaluation = await svc.evaluate_host(db_session, fresh, guard=guard)
    assert evaluation.alive is True  # grace window from created_at

    old = Host(hostname="old-host", ip="10.0.0.5", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(old)
    await db_session.flush()
    old.created_at = now_utc() - timedelta(minutes=10)
    await db_session.flush()

    svc2 = _hb_svc(db_session)
    guard2 = _unguarded_guard(svc2)
    evaluation2 = await svc2.evaluate_host(db_session, old, guard=guard2)
    assert evaluation2.alive is False
    assert old.status == HostStatus.offline


async def test_fresh_push_flips_ledger_online_and_emits_once(db_session: AsyncSession) -> None:
    """Recovery edge: ledger offline + fresh push -> flip online, one event, then quiet."""
    host = Host(
        hostname="recovered-host", ip="10.0.0.7", os_type=OSType.linux, agent_port=5100, status=HostStatus.offline
    )
    host.last_heartbeat = now_utc()
    db_session.add(host)
    await db_session.commit()

    publisher = Mock()
    svc = _hb_svc(db_session, publisher=publisher)
    guard = _unguarded_guard(svc)
    evaluation = await svc.evaluate_host(db_session, host, guard=guard)

    assert evaluation.alive is True
    assert host.status == HostStatus.online
    changed = [c for c in publisher.queue_for_session.call_args_list if c.args[1] == "host.status_changed"]
    assert len(changed) == 1
    assert changed[0].args[2]["new_status"] == "online"

    # Second cycle, same freshness: ledger already online -> no second event.
    await svc.evaluate_host(db_session, host, guard=svc.begin_cycle())
    changed_again = [c for c in publisher.queue_for_session.call_args_list if c.args[1] == "host.status_changed"]
    assert len(changed_again) == 1


async def test_stale_then_stale_emits_one_offline_pair(db_session: AsyncSession) -> None:
    """Offline edge fires once: cycle 1 flips + emits, cycle 2 (still stale) is quiet."""
    host = Host(
        hostname="stale-pair-host", ip="10.0.0.8", os_type=OSType.linux, agent_port=5100, status=HostStatus.online
    )
    host.last_heartbeat = now_utc() - timedelta(minutes=10)
    db_session.add(host)
    await db_session.commit()

    publisher = Mock()
    svc = _hb_svc(db_session, publisher=publisher)
    guard = _unguarded_guard(svc)
    await svc.evaluate_host(db_session, host, guard=guard)
    await db_session.commit()

    assert host.status == HostStatus.offline
    names = [c.args[1] for c in publisher.queue_for_session.call_args_list]
    assert names.count("host.status_changed") == 1
    assert names.count("host.heartbeat_lost") == 1

    # Cycle 2, still stale, ledger already offline -> no new events.
    await svc.evaluate_host(db_session, host, guard=svc.begin_cycle())
    names = [c.args[1] for c in publisher.queue_for_session.call_args_list]
    assert names.count("host.status_changed") == 1
    assert names.count("host.heartbeat_lost") == 1


async def test_never_pushed_offline_host_emits_no_online_edge(db_session: AsyncSession) -> None:
    """Operator-created row: ledger offline, no heartbeat, fresh created_at. The
    fresh branch runs (created_at grace) but the edge's last_heartbeat guard holds."""
    host = Host(
        hostname="operator-host", ip="10.0.0.9", os_type=OSType.linux, agent_port=5100, status=HostStatus.offline
    )
    db_session.add(host)
    await db_session.commit()
    assert host.last_heartbeat is None

    publisher = Mock()
    svc = _hb_svc(db_session, publisher=publisher)
    guard = _unguarded_guard(svc)
    evaluation = await svc.evaluate_host(db_session, host, guard=guard)

    assert evaluation.alive is True  # created_at grace
    assert host.status == HostStatus.offline  # no flip
    assert publisher.queue_for_session.call_args_list == []


async def test_alive_evaluation_ingests_restart_events_from_snapshot_once(db_session: AsyncSession) -> None:
    """Cursor pin: seed the snapshot with one restart event, evaluate twice ->
    exactly one DeviceEvent (sequence cursor dedupes re-reads of the same snapshot)."""
    host = Host(hostname="cursor-host", ip="10.0.0.6", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-cursor",
        connection_target="dev-cursor",
        name="Cursor Device",
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
        pid=1111,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        active_connection_target="",
    )
    db_session.add(node)
    await db_session.commit()

    await _seed_snapshot(
        db_session,
        host,
        {
            "recent_restart_events": [
                {
                    "sequence": 1,
                    "kind": "crash_detected",
                    "port": 4723,
                    "pid": 1111,
                    "attempt": 1,
                    "will_retry": True,
                }
            ]
        },
    )

    svc = _hb_svc(db_session)
    for _ in range(2):
        guard = svc.begin_cycle()
        await svc.evaluate_host(db_session, host, guard=guard)
        await db_session.commit()

    crash_events = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == device.id, DeviceEvent.event_type == DeviceEventType.node_crash
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(crash_events) == 1
    assert await control_plane_state_store.get_value(db_session, APPIUM_RESTART_SEQUENCE_NAMESPACE, str(host.id)) == 1


async def test_partition_probe_records_metric_and_log_without_state_change(db_session: AsyncSession) -> None:
    """Network-partition case: pushes fresh (alive) but probe_host gets connect_error ->
    host stays online; gridfleet_agent_heartbeat_total incremented with the failure
    outcome; 'agent_partition_suspected' warning logged."""
    host = Host(hostname="probe-host", ip="10.0.0.7", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.commit()

    connect_error = HeartbeatPingResult(
        outcome=HeartbeatOutcome.connect_error,
        payload=None,
        duration_ms=3,
        client_mode=ClientMode.pooled,
        http_status=None,
        error_category="ConnectError",
    )
    before = HEARTBEAT_PING_TOTAL.labels(
        host_id=str(host.id), outcome="connect_error", client_mode="pooled"
    )._value.get()  # type: ignore[attr-defined]

    svc = _hb_svc(db_session)
    with (
        patch("app.appium_nodes.services.heartbeat._ping_agent", new=AsyncMock(return_value=connect_error)),
        structlog.testing.capture_logs() as cap,
    ):
        result = await svc.probe_host(host_id=str(host.id), host_ip=host.ip, agent_port=host.agent_port)

    assert result.outcome is HeartbeatOutcome.connect_error
    after = HEARTBEAT_PING_TOTAL.labels(
        host_id=str(host.id), outcome="connect_error", client_mode="pooled"
    )._value.get()  # type: ignore[attr-defined]
    assert after == before + 1
    await db_session.refresh(host)
    assert host.status == HostStatus.online  # probe writes no state
    assert any(e.get("event") == "agent_partition_suspected" for e in cap)


# ─────────────── canonical availability event on the offline cascade ───────────────


async def test_host_offline_cascade_publishes_canonical_availability_event(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict[str, object], str | None]] = []

    async def fake_publish(name: str, payload: dict[str, object], *, severity: str | None = None) -> None:
        captured.append((name, payload, severity))

    monkeypatch.setattr(test_event_bus, "publish", fake_publish)

    host = Host(
        hostname="cascade-host",
        ip="10.0.0.42",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    host.last_heartbeat = now_utc() - timedelta(minutes=10)
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
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    svc = _hb_svc(db_session, publisher=test_event_bus)
    guard = _unguarded_guard(svc)
    await svc.evaluate_host(db_session, host, guard=guard)
    await db_session.commit()

    availability_events = [
        (payload, severity) for name, payload, severity in captured if name == "device.operational_state_changed"
    ]
    cascade_events = [
        event
        for event in availability_events
        if event[0].get("device_id") == str(device.id) and event[0].get("new_operational_state") == "offline"
    ]
    assert len(cascade_events) == 1, (
        f"Expected exactly one cascade event for device, got {len(cascade_events)}: {cascade_events}"
    )
    payload, severity = cascade_events[0]
    assert payload["old_operational_state"] == "available"
    assert payload["new_operational_state"] == "offline"
    assert "reason" not in payload
    assert severity == "warning"


# ─────────────────────── restart-event ingest (behavior pins) ───────────────────────


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
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=1111,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        active_connection_target="",
    )
    db_session.add(node)
    await db_session.commit()

    await set_node_health_failure_count(db_session, str(node.id), 2)
    await _seed_snapshot(
        db_session,
        host,
        {
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
    )

    svc = _hb_svc(db_session)
    for _ in range(2):
        guard = svc.begin_cycle()
        await svc.evaluate_host(db_session, host, guard=guard)
        await db_session.commit()

    await db_session.refresh(node)
    assert node.pid == 2222
    assert node.observed_running
    assert await control_plane_state_store.get_value(db_session, APPIUM_RESTART_SEQUENCE_NAMESPACE, str(host.id)) == 2
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
    device_reloaded = (
        await db_session.execute(select(Device).where(Device.id == device.id).options(selectinload(Device.appium_node)))
    ).scalar_one()
    assert device_health.build_public_summary(device_reloaded)["node"]["status"] == "ok"


async def test_restart_succeeded_eager_fills_active_connection_target(db_session: AsyncSession) -> None:
    """I11/N15: after a crash auto-restart, a reconciler poll that observed the down window may
    have nulled ``active_connection_target``. The agent confirms the node is back via
    ``restart_succeeded``, so the handler must restore the node-viability marker immediately."""
    host = Host(
        hostname="agent-host-act", ip="10.0.0.9", os_type=OSType.linux, agent_port=5100, status=HostStatus.online
    )
    db_session.add(host)
    await db_session.flush()

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-act-1",
        connection_target="dev-act-1",
        name="Eager Fill Phone",
        os_version="14",
        host_id=host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=None,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        active_connection_target=None,
    )
    db_session.add(node)
    await db_session.commit()

    await _seed_snapshot(
        db_session,
        host,
        {
            "running_nodes": [],
            "recent_restart_events": [
                {
                    "sequence": 1,
                    "kind": "restart_succeeded",
                    "port": 4723,
                    "pid": 2222,
                    "attempt": 1,
                    "occurred_at": "2026-04-04T10:00:01+00:00",
                    "will_retry": False,
                }
            ],
        },
    )

    svc = _hb_svc(db_session)
    guard = svc.begin_cycle()
    await svc.evaluate_host(db_session, host, guard=guard)
    await db_session.commit()

    await db_session.refresh(node)
    assert node.pid == 2222
    assert node.active_connection_target is not None
    assert node.observed_running


@pytest.mark.usefixtures("seeded_driver_packs")
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
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4724,
        pid=3333,
        desired_state=AppiumDesiredState.running,
        desired_port=4724,
        active_connection_target="",
        health_running=True,
    )
    db_session.add(node)
    await db_session.commit()

    await _seed_snapshot(
        db_session,
        host,
        {
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
    )

    svc = _hb_svc(db_session)
    guard = svc.begin_cycle()
    await svc.evaluate_host(db_session, host, guard=guard)
    await db_session.commit()

    await db_session.refresh(node)
    await db_session.refresh(device)
    assert node.observed_running
    assert device.operational_state_last_emitted is DeviceOperationalState.available

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
    await NodeHealthService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        recovery_control=AsyncMock(),
        health=DeviceHealthService(publisher=Mock()),
        incidents=AsyncMock(),
    ).fold_host_nodes(
        db_session,
        device.host_id,
        {
            "reported_at": now_utc().isoformat(),
            "nodes": [
                {
                    "port": node.port,
                    "pid": node.pid,
                    "connection_target": node.active_connection_target,
                    "running": False,
                    "observed_at": now_utc().isoformat(),
                }
            ],
        },
    )

    await db_session.refresh(node)
    assert node.observed_running is True
    assert node.health_state == "error"
    assert node.desired_state == AppiumDesiredState.running
    assert node.restart_requested_at is not None


async def test_unknown_process_restart_events_normalize_to_appium_and_restore_health_summary(
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
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4725,
        pid=4444,
        desired_state=AppiumDesiredState.running,
        desired_port=4725,
        active_connection_target="",
    )
    db_session.add(node)
    await db_session.commit()

    await set_node_health_failure_count(db_session, str(node.id), 2)
    await _seed_snapshot(
        db_session,
        host,
        {
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
    )

    svc = _hb_svc(db_session)
    guard = svc.begin_cycle()
    await svc.evaluate_host(db_session, host, guard=guard)
    await db_session.commit()

    await db_session.refresh(node)
    assert node.pid == 8888
    assert node.observed_running
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
    event_types = [event.event_type for event in events]
    assert DeviceEventType.node_crash in event_types
    assert DeviceEventType.node_restart in event_types
    crash_event = next(e for e in events if e.event_type == DeviceEventType.node_crash)
    restart_event = next(e for e in events if e.event_type == DeviceEventType.node_restart)
    assert crash_event.details is not None
    assert crash_event.details["process"] == "appium"
    assert restart_event.details is not None
    assert restart_event.details["process"] == "appium"

    await db_session.refresh(node)
    assert node.health_running is None
    assert node.health_state is None
    device_reloaded = (
        await db_session.execute(select(Device).where(Device.id == device.id).options(selectinload(Device.appium_node)))
    ).scalar_one()
    assert device_health.build_public_summary(device_reloaded)["node"]["status"] == "ok"


async def test_restart_exhausted_sets_degraded_state(
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
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4726,
        pid=5555,
        desired_state=AppiumDesiredState.running,
        desired_port=4726,
        active_connection_target="",
    )
    db_session.add(node)
    await db_session.commit()

    await _seed_snapshot(
        db_session,
        host,
        {
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
    )

    svc = _hb_svc(db_session)
    guard = svc.begin_cycle()
    await svc.evaluate_host(db_session, host, guard=guard)
    await db_session.commit()

    await db_session.refresh(node)
    assert node.health_running is False
    assert node.health_state == "restart_exhausted"
