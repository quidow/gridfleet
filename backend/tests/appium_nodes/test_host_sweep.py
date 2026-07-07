from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import heartbeat as heartbeat_module
from app.appium_nodes.services import reconciler as reconciler_module
from app.appium_nodes.services.heartbeat import HeartbeatService
from app.appium_nodes.services.heartbeat_outcomes import ClientMode, HeartbeatOutcome, HeartbeatPingResult
from app.appium_nodes.services.host_sweep import run_host_sweep_once, stage_due
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
