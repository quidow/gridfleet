from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import heartbeat as heartbeat_module
from app.appium_nodes.services import reconciler as reconciler_module
from app.appium_nodes.services.heartbeat import HeartbeatService
from app.appium_nodes.services.heartbeat_outcomes import ClientMode, HeartbeatOutcome, HeartbeatPingResult
from app.appium_nodes.services.host_sweep import (
    OBSERVATION_FOLD_NAMESPACE,
    ObservationFold,
    run_host_sweep_once,
    stage_due,
)
from app.appium_nodes.services.reconciler import ReconcilerService
from app.core.leader import state_store as control_plane_state_store
from app.core.timeutil import now_utc
from app.devices.models import DeviceOperationalState
from app.hosts.service_status_push import HOST_STATUS_NAMESPACE
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


async def test_sweep_converges_from_stored_snapshot_without_fetch(
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
    # The push handler already stored this snapshot; the sweep must converge from
    # it without a second /agent/health fetch.
    await control_plane_state_store.set_value(
        db_session,
        HOST_STATUS_NAMESPACE,
        str(db_host.id),
        {
            "received_at": now_utc().isoformat(),
            "payload": {
                "appium_processes": {
                    "running_nodes": [
                        {"port": 4723, "pid": 123, "connection_target": "host-sweep-target", "platform_id": "android"}
                    ]
                }
            },
        },
    )
    await db_session.commit()

    # No fetch drives aliveness or convergence anymore; the probe path still dials
    # the agent, so stub it to keep the test off the network.
    monkeypatch.setattr(heartbeat_module, "_ping_agent", AsyncMock(return_value=_alive_ping()))
    monkeypatch.setattr(reconciler_module, "_touch_last_observed", AsyncMock())
    converge = AsyncMock()
    monkeypatch.setattr(ReconcilerService, "converge_host_rows", converge)
    settings = FakeSettingsReader()

    await run_host_sweep_once(
        db_session,
        heartbeat=_heartbeat_service(settings=settings, session_factory=db_session_maker),
        reconciler=_reconciler_service(settings=settings, session_factory=db_session_maker),
        settings=settings,
        session_factory=db_session_maker,
    )

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
    # Dead = no status push within the offline window; recency, not a failed ping.
    db_host.last_heartbeat = now_utc() - timedelta(minutes=10)
    await db_session.commit()
    monkeypatch.setattr(heartbeat_module, "_ping_agent", AsyncMock(return_value=_dead_ping()))
    reconcile_host = AsyncMock()
    monkeypatch.setattr(ReconcilerService, "reconcile_host", reconcile_host)
    settings = FakeSettingsReader()

    await run_host_sweep_once(
        db_session,
        heartbeat=_heartbeat_service(settings=settings, session_factory=db_session_maker),
        reconciler=_reconciler_service(settings=settings, session_factory=db_session_maker),
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
    db_host: Host,
    calls: list[object],
    cycle_index: int,
    alive: bool = True,
    reconcile_raises: bool = False,
    fold_raises: bool = False,
    record_cooldowns: bool = False,
    include_node_health: bool = True,
    node_health_reported_at: str = "2026-07-10T00:00:00+00:00",
) -> None:
    """Drive one sweep with convergence / observation-fold / connectivity recorders.

    Aliveness derives from status-push recency: an alive host gets a fresh stored
    snapshot (so convergence has a payload to consume); a dead host gets a stale
    ``last_heartbeat``.
    """
    if alive:
        payload: dict[str, object] = {"appium_processes": {}, "host_telemetry": {}}
        if include_node_health:
            payload["node_health"] = {"reported_at": node_health_reported_at, "nodes": []}
        await control_plane_state_store.set_value(
            db_session,
            HOST_STATUS_NAMESPACE,
            str(db_host.id),
            {"received_at": now_utc().isoformat(), "payload": payload},
        )
    else:
        db_host.last_heartbeat = now_utc() - timedelta(minutes=10)
    # Commit so the host is visible to the fresh sessions _sweep_host opens.
    await db_session.commit()
    settings = FakeSettingsReader()
    # The probe path still dials the agent on its cadence; stub it off the network.
    monkeypatch.setattr(heartbeat_module, "_ping_agent", AsyncMock(return_value=_alive_ping()))

    async def _record_reconcile(**_kwargs: object) -> None:
        calls.append("reconcile_host")
        if reconcile_raises:
            raise RuntimeError("convergence boom")

    async def _record_fold(_db: object, _host_id: object, section: dict[str, object]) -> None:
        calls.append(("fold", section["reported_at"]))
        if fold_raises:
            raise RuntimeError("fold boom")

    async def _record_cooldowns(_db: object) -> None:
        calls.append("expire_cooldowns")

    monkeypatch.setattr(ReconcilerService, "reconcile_host", AsyncMock(side_effect=_record_reconcile))
    fold = ObservationFold("node_health", _record_fold)

    await run_host_sweep_once(
        db_session,
        heartbeat=_heartbeat_service(settings=settings, session_factory=db_session_maker),
        reconciler=_reconciler_service(settings=settings, session_factory=db_session_maker),
        settings=settings,
        session_factory=db_session_maker,
        observation_folds=(fold,),
        expire_cooldowns=_record_cooldowns if record_cooldowns else None,
        cycle_index=cycle_index,
    )


async def test_fold_runs_once_per_stamp(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    await _run_sweep_with_recorders(
        monkeypatch,
        db_session=db_session,
        db_session_maker=db_session_maker,
        db_host=db_host,
        calls=calls,
        cycle_index=1,
    )
    await _run_sweep_with_recorders(
        monkeypatch,
        db_session=db_session,
        db_session_maker=db_session_maker,
        db_host=db_host,
        calls=calls,
        cycle_index=1,
    )
    await _run_sweep_with_recorders(
        monkeypatch,
        db_session=db_session,
        db_session_maker=db_session_maker,
        db_host=db_host,
        calls=calls,
        cycle_index=1,
        node_health_reported_at="2026-07-10T00:00:01+00:00",
    )
    assert calls == [
        "reconcile_host",
        ("fold", "2026-07-10T00:00:00+00:00"),
        "reconcile_host",
        "reconcile_host",
        ("fold", "2026-07-10T00:00:01+00:00"),
    ]
    marks = await control_plane_state_store.get_value(db_session, OBSERVATION_FOLD_NAMESPACE, str(db_host.id))
    assert marks == {"node_health": "2026-07-10T00:00:01+00:00"}


async def test_fold_skipped_when_section_missing(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    await _run_sweep_with_recorders(
        monkeypatch,
        db_session=db_session,
        db_session_maker=db_session_maker,
        db_host=db_host,
        calls=calls,
        cycle_index=1,
        include_node_health=False,
    )
    assert "reconcile_host" in calls
    assert not any(call[0] == "fold" for call in calls if isinstance(call, tuple))
    assert await control_plane_state_store.get_value(db_session, OBSERVATION_FOLD_NAMESPACE, str(db_host.id)) is None


async def test_fold_skipped_for_dead_host(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    # A dead host leaves no per-host calls; connectivity is off-cycle.
    await _run_sweep_with_recorders(
        monkeypatch,
        db_session=db_session,
        db_session_maker=db_session_maker,
        db_host=db_host,
        calls=calls,
        cycle_index=1,
        alive=False,
    )
    assert calls == []


async def test_fold_failure_does_not_advance_watermark(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    # Stage isolation: a failed fold is retried on the next cycle while
    # convergence continues to run.
    await _run_sweep_with_recorders(
        monkeypatch,
        db_session=db_session,
        db_session_maker=db_session_maker,
        db_host=db_host,
        calls=calls,
        cycle_index=1,
        fold_raises=True,
    )
    assert await control_plane_state_store.get_value(db_session, OBSERVATION_FOLD_NAMESPACE, str(db_host.id)) is None
    await _run_sweep_with_recorders(
        monkeypatch,
        db_session=db_session,
        db_session_maker=db_session_maker,
        db_host=db_host,
        calls=calls,
        cycle_index=1,
    )
    assert calls == [
        "reconcile_host",
        ("fold", "2026-07-10T00:00:00+00:00"),
        "reconcile_host",
        ("fold", "2026-07-10T00:00:00+00:00"),
    ]
    assert await control_plane_state_store.get_value(db_session, OBSERVATION_FOLD_NAMESPACE, str(db_host.id)) == {
        "node_health": "2026-07-10T00:00:00+00:00"
    }


async def test_fold_runs_after_convergence(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    await _run_sweep_with_recorders(
        monkeypatch,
        db_session=db_session,
        db_session_maker=db_session_maker,
        db_host=db_host,
        calls=calls,
        cycle_index=1,
    )
    assert calls == ["reconcile_host", ("fold", "2026-07-10T00:00:00+00:00")]


async def test_expire_cooldowns_runs_every_cycle_even_when_off_cadence(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cooldown expiry is a direct once-per-cycle call with no cadence gate.
    calls: list[object] = []
    await _run_sweep_with_recorders(
        monkeypatch,
        db_session=db_session,
        db_session_maker=db_session_maker,
        db_host=db_host,
        calls=calls,
        cycle_index=1,
        record_cooldowns=True,
    )
    assert "expire_cooldowns" in calls


async def test_expire_cooldowns_runs_with_zero_alive_hosts(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # DB-only cleanup must run even when no host is alive (nothing else happens).
    calls: list[object] = []
    await _run_sweep_with_recorders(
        monkeypatch,
        db_session=db_session,
        db_session_maker=db_session_maker,
        db_host=db_host,
        calls=calls,
        cycle_index=1,
        alive=False,
        record_cooldowns=True,
    )
    assert calls == ["expire_cooldowns"]


async def test_probe_stage_gated_by_partition_probe_interval(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The network-partition probe dials the agent only on its cadence cycles.
    Default 60s partition_probe / 15s base → divisor 4 → probe on cycles 0 and 4."""
    await db_session.commit()  # db_host is fresh + online → alive by recency
    settings = FakeSettingsReader()
    probe_ips: list[str] = []

    async def _record_ping(ip: str, port: int, **_kwargs: object) -> HeartbeatPingResult:
        probe_ips.append(ip)
        return _alive_ping()

    monkeypatch.setattr(heartbeat_module, "_ping_agent", AsyncMock(side_effect=_record_ping))
    monkeypatch.setattr(ReconcilerService, "reconcile_host", AsyncMock())

    for cycle_index, expected in ((0, True), (1, False), (2, False), (3, False), (4, True)):
        probe_ips.clear()
        await run_host_sweep_once(
            db_session,
            heartbeat=_heartbeat_service(settings=settings, session_factory=db_session_maker),
            reconciler=_reconciler_service(settings=settings, session_factory=db_session_maker),
            settings=settings,
            session_factory=db_session_maker,
            cycle_index=cycle_index,
        )
        assert (db_host.ip in probe_ips) is expected
