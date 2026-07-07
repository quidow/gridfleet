from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import heartbeat as heartbeat_module
from app.appium_nodes.services import reconciler as reconciler_module
from app.appium_nodes.services.heartbeat import HeartbeatService
from app.appium_nodes.services.heartbeat_outcomes import ClientMode, HeartbeatOutcome, HeartbeatPingResult
from app.appium_nodes.services.host_sweep import SweepStage, run_host_sweep_once, stage_due
from app.appium_nodes.services.reconciler import ReconcilerService
from app.devices.models import DeviceOperationalState
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host


def _heartbeat_service(
    *, settings: FakeSettingsReader, session_factory: async_sessionmaker[AsyncSession]
) -> HeartbeatService:
    return HeartbeatService(
        publisher=event_bus,
        settings=settings,
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=session_factory,
    )


def _reconciler_service(
    *, settings: FakeSettingsReader, session_factory: async_sessionmaker[AsyncSession]
) -> ReconcilerService:
    return ReconcilerService(
        publisher=event_bus,
        settings=settings,
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=session_factory,
    )


async def test_sweep_converges_from_ping_payload_without_second_fetch(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Host Sweep Device",
        identity_value="host-sweep-001",
        connection_target="host-sweep-target",
        operational_state=DeviceOperationalState.available,
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )
    )
    await db_session.commit()

    payload = {
        "appium_processes": {
            "running_nodes": [
                {"port": 4723, "pid": 123, "connection_target": "host-sweep-target", "platform_id": "android"}
            ]
        }
    }
    ping = HeartbeatPingResult(
        outcome=HeartbeatOutcome.success,
        payload=payload,
        duration_ms=1,
        client_mode=ClientMode.pooled,
        http_status=200,
        error_category=None,
    )
    monkeypatch.setattr(heartbeat_module, "_ping_agent", AsyncMock(return_value=ping))
    agent_health = AsyncMock(return_value={})
    monkeypatch.setattr(reconciler_module, "agent_health", agent_health)
    monkeypatch.setattr(reconciler_module, "_touch_last_observed", AsyncMock())
    monkeypatch.setattr(reconciler_module, "reap_orphan_nodes", AsyncMock())
    converge = AsyncMock()
    monkeypatch.setattr(ReconcilerService, "converge_host_rows", converge)
    settings = FakeSettingsReader()

    await run_host_sweep_once(
        db_session,
        heartbeat=_heartbeat_service(settings=settings, session_factory=db_session_maker),
        reconciler=_reconciler_service(settings=settings, session_factory=db_session_maker),
        node_health=Mock(check_host_nodes=AsyncMock()),
        settings=settings,
        session_factory=db_session_maker,
    )

    agent_health.assert_not_awaited()
    converge.assert_awaited_once()
    observed = converge.await_args.args[2]
    assert [(entry.port, entry.pid, entry.connection_target) for entry in observed] == [
        (4723, 123, "host-sweep-target")
    ]


async def test_sweep_skips_convergence_for_dead_host(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await db_session.commit()
    ping = HeartbeatPingResult(
        outcome=HeartbeatOutcome.timeout,
        payload=None,
        duration_ms=1,
        client_mode=ClientMode.pooled,
        http_status=None,
        error_category="Timeout",
    )
    monkeypatch.setattr(heartbeat_module, "_ping_agent", AsyncMock(return_value=ping))
    reconcile_host = AsyncMock()
    monkeypatch.setattr(ReconcilerService, "reconcile_host", reconcile_host)
    settings = FakeSettingsReader()

    await run_host_sweep_once(
        db_session,
        heartbeat=_heartbeat_service(settings=settings, session_factory=db_session_maker),
        reconciler=_reconciler_service(settings=settings, session_factory=db_session_maker),
        node_health=Mock(check_host_nodes=AsyncMock()),
        settings=settings,
        session_factory=db_session_maker,
    )

    reconcile_host.assert_not_awaited()


def test_stage_due_divisor_rounding() -> None:
    # 30s stage on a 15s base: every second cycle.
    assert stage_due(0, base_interval=15.0, stage_interval=30.0) is True
    assert stage_due(1, base_interval=15.0, stage_interval=30.0) is False
    assert stage_due(2, base_interval=15.0, stage_interval=30.0) is True
    # Stage interval at or below base: every cycle, never a zero divisor.
    assert stage_due(7, base_interval=15.0, stage_interval=15.0) is True
    assert stage_due(7, base_interval=15.0, stage_interval=1.0) is True
    # 60s stage: every fourth cycle.
    assert stage_due(4, base_interval=15.0, stage_interval=60.0) is True
    assert stage_due(5, base_interval=15.0, stage_interval=60.0) is False


def _alive_ping() -> HeartbeatPingResult:
    return HeartbeatPingResult(
        outcome=HeartbeatOutcome.success,
        payload={},
        duration_ms=1,
        client_mode=ClientMode.pooled,
        http_status=200,
        error_category=None,
    )


def _dead_ping() -> HeartbeatPingResult:
    return HeartbeatPingResult(
        outcome=HeartbeatOutcome.timeout,
        payload=None,
        duration_ms=1,
        client_mode=ClientMode.pooled,
        http_status=None,
        error_category="Timeout",
    )


async def _run_sweep_with_recorders(
    monkeypatch: pytest.MonkeyPatch,
    *,
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    calls: list[str],
    cycle_index: int,
    alive: bool = True,
    reconcile_raises: bool = False,
    connectivity_raises: bool = False,
) -> None:
    """Drive one sweep with reconcile_host / check_host_nodes / connectivity recorders."""
    # Commit so the host is visible to the fresh sessions _sweep_host opens.
    await db_session.commit()
    settings = FakeSettingsReader()
    monkeypatch.setattr(
        heartbeat_module, "_ping_agent", AsyncMock(return_value=_alive_ping() if alive else _dead_ping())
    )

    async def _record_reconcile(**_kwargs: object) -> None:
        calls.append("reconcile_host")
        if reconcile_raises:
            raise RuntimeError("convergence boom")

    async def _record_health(_db: object, *, host_id: object) -> None:
        _ = host_id
        calls.append("check_host_nodes")

    async def _record_connectivity(_db: object) -> None:
        calls.append("run_connectivity_pass")
        if connectivity_raises:
            raise RuntimeError("connectivity boom")

    monkeypatch.setattr(ReconcilerService, "reconcile_host", AsyncMock(side_effect=_record_reconcile))
    node_health = Mock()
    node_health.check_host_nodes = AsyncMock(side_effect=_record_health)
    connectivity_stage = SweepStage("connectivity", "general.device_check_interval_sec", _record_connectivity)

    await run_host_sweep_once(
        db_session,
        heartbeat=_heartbeat_service(settings=settings, session_factory=db_session_maker),
        reconciler=_reconciler_service(settings=settings, session_factory=db_session_maker),
        settings=settings,
        session_factory=db_session_maker,
        node_health=node_health,
        global_stages=(connectivity_stage,),
        cycle_index=cycle_index,
    )


async def test_node_health_stage_runs_on_due_cycle_after_convergence(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    # Cycle 2: node health due (divisor 2), connectivity not (divisor 4) — isolates node health.
    await _run_sweep_with_recorders(
        monkeypatch, db_session=db_session, db_session_maker=db_session_maker, calls=calls, cycle_index=2
    )
    assert calls == ["reconcile_host", "check_host_nodes"]


async def test_node_health_stage_skipped_on_off_cycle(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    # Default 30s/15s settings → divisor 2 → node health only on even cycles.
    await _run_sweep_with_recorders(
        monkeypatch, db_session=db_session, db_session_maker=db_session_maker, calls=calls, cycle_index=1
    )
    assert "reconcile_host" in calls
    assert "check_host_nodes" not in calls


async def test_node_health_stage_skipped_for_dead_host(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    # Cycle 2: connectivity not due (divisor 4), so a dead host leaves calls empty.
    await _run_sweep_with_recorders(
        monkeypatch, db_session=db_session, db_session_maker=db_session_maker, calls=calls, cycle_index=2, alive=False
    )
    assert calls == []


async def test_node_health_stage_runs_even_when_convergence_fails(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    # Stage isolation: a convergence failure must not skip node health on an alive host.
    # Cycle 2: node health due, connectivity not — isolates the node-health path.
    await _run_sweep_with_recorders(
        monkeypatch,
        db_session=db_session,
        db_session_maker=db_session_maker,
        calls=calls,
        cycle_index=2,
        reconcile_raises=True,
    )
    assert calls == ["reconcile_host", "check_host_nodes"]


async def test_connectivity_stage_runs_after_fanout_on_due_cycle(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    await _run_sweep_with_recorders(
        monkeypatch, db_session=db_session, db_session_maker=db_session_maker, calls=calls, cycle_index=0
    )
    # Global stage runs strictly after every per-host stage.
    assert calls[-1] == "run_connectivity_pass"


async def test_connectivity_stage_skipped_on_off_cycles(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default 60s/15s settings → divisor 4 → connectivity only on cycles 0 and 4.
    for cycle_index, expected in ((1, False), (2, False), (3, False), (4, True)):
        calls: list[str] = []
        await _run_sweep_with_recorders(
            monkeypatch, db_session=db_session, db_session_maker=db_session_maker, calls=calls, cycle_index=cycle_index
        )
        assert ("run_connectivity_pass" in calls) is expected


async def test_connectivity_stage_failure_does_not_fail_the_cycle(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    # Stage isolation: a connectivity failure must not raise out of the sweep cycle.
    await _run_sweep_with_recorders(
        monkeypatch,
        db_session=db_session,
        db_session_maker=db_session_maker,
        calls=calls,
        cycle_index=0,
        connectivity_raises=True,
    )
    assert "run_connectivity_pass" in calls
