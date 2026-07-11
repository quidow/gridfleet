from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from app.appium_nodes.services import heartbeat as heartbeat_module
from app.appium_nodes.services.heartbeat import HeartbeatService
from app.appium_nodes.services.heartbeat_outcomes import ClientMode, HeartbeatOutcome, HeartbeatPingResult
from app.appium_nodes.services.host_sweep import run_host_sweep_once, stage_due
from app.appium_nodes.services.reconciler import ReconcilerService
from app.core.timeutil import now_utc
from tests.fakes import FakeSettingsReader
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


async def test_sweep_skips_partition_probe_for_dead_host(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_host.last_heartbeat = now_utc() - timedelta(minutes=10)
    await db_session.commit()
    ping = AsyncMock(return_value=_dead_ping())
    monkeypatch.setattr(heartbeat_module, "_ping_agent", ping)

    await run_host_sweep_once(
        db_session,
        heartbeat=_heartbeat_service(settings=FakeSettingsReader(), session_factory=db_session_maker),
        reconciler=_reconciler_service(settings=FakeSettingsReader(), session_factory=db_session_maker),
        settings=FakeSettingsReader(),
        session_factory=db_session_maker,
    )

    ping.assert_not_awaited()


async def test_sweep_evaluation_has_no_push_snapshot_payload(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    heartbeat = _heartbeat_service(settings=FakeSettingsReader(), session_factory=db_session_maker)
    evaluation = await heartbeat.evaluate_host(db_session, db_host, guard=heartbeat.begin_cycle())

    assert evaluation.alive is True
    assert not hasattr(evaluation, "payload")


def test_stage_due_divisor_rounding() -> None:
    assert stage_due(0, base_interval=15.0, stage_interval=30.0) is True
    assert stage_due(1, base_interval=15.0, stage_interval=30.0) is False
    assert stage_due(2, base_interval=15.0, stage_interval=30.0) is True
    assert stage_due(7, base_interval=15.0, stage_interval=15.0) is True
    assert stage_due(7, base_interval=15.0, stage_interval=1.0) is True
    assert stage_due(4, base_interval=15.0, stage_interval=60.0) is True
    assert stage_due(5, base_interval=15.0, stage_interval=60.0) is False


async def _run_sweep_with_cooldown_recorder(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    *,
    alive: bool,
) -> None:
    if alive:
        db_host.last_heartbeat = now_utc()
    else:
        db_host.last_heartbeat = now_utc() - timedelta(minutes=10)
    await db_session.commit()
    monkeypatch.setattr(heartbeat_module, "_ping_agent", AsyncMock(return_value=_alive_ping()))

    async def expire_cooldowns(_db: AsyncSession) -> None:
        calls.append("expire_cooldowns")

    await run_host_sweep_once(
        db_session,
        heartbeat=_heartbeat_service(settings=FakeSettingsReader(), session_factory=db_session_maker),
        reconciler=_reconciler_service(settings=FakeSettingsReader(), session_factory=db_session_maker),
        settings=FakeSettingsReader(),
        session_factory=db_session_maker,
        expire_cooldowns=expire_cooldowns,
    )


async def test_expire_cooldowns_runs_every_cycle(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    await _run_sweep_with_cooldown_recorder(db_session, db_session_maker, db_host, monkeypatch, calls, alive=True)

    assert calls == ["expire_cooldowns"]


async def test_expire_cooldowns_runs_with_zero_alive_hosts(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    await _run_sweep_with_cooldown_recorder(db_session, db_session_maker, db_host, monkeypatch, calls, alive=False)

    assert calls == ["expire_cooldowns"]


async def test_probe_stage_gated_by_partition_probe_interval(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await db_session.commit()
    settings = FakeSettingsReader()
    probe_ips: list[str] = []

    async def record_ping(ip: str, port: int, **_kwargs: object) -> HeartbeatPingResult:
        probe_ips.append(ip)
        return _alive_ping()

    monkeypatch.setattr(heartbeat_module, "_ping_agent", AsyncMock(side_effect=record_ping))
    heartbeat = _heartbeat_service(settings=settings, session_factory=db_session_maker)
    reconciler = _reconciler_service(settings=settings, session_factory=db_session_maker)

    for cycle_index, expected in ((0, True), (1, False), (2, False), (3, False), (4, True)):
        probe_ips.clear()
        await run_host_sweep_once(
            db_session,
            heartbeat=heartbeat,
            reconciler=reconciler,
            settings=settings,
            session_factory=db_session_maker,
            cycle_index=cycle_index,
        )
        assert (db_host.ip in probe_ips) is expected
