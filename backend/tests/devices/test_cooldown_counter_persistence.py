"""Cooldown counter must persist across exclusion-clear (TTL expiry) cycles.

If the counter resets every time the cooldown:reservation intent expires,
the escalation threshold becomes unreachable for slow-burn flakes where
each timeout TTL window only sees one failure. The counter is the
authority on "how many cooldowns this reservation has seen"; it is reset
only when the reservation is released or explicitly restored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.agent_comm.circuit_breaker import AgentCircuitBreaker
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import DeviceReservation, ExclusionKind
from app.devices.services.intent_reconciler import ReconcileCandidate, reconcile_device_command
from app.devices.services.maintenance import MaintenanceService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.runs.service_lifecycle_failures import RunFailureService
from app.runs.service_reservation import RunReservationService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device, create_reserved_run
from tests.helpers import test_event_bus as event_bus

_settings = FakeSettingsReader({})
_circuit_breaker = AgentCircuitBreaker(publisher=event_bus)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host


def _make_failure_svc(session_factory: async_sessionmaker[AsyncSession]) -> RunFailureService:
    return RunFailureService(
        publisher=event_bus,
        settings=_settings,
        circuit_breaker=_circuit_breaker,
        maintenance=MaintenanceService(
            review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
        ),
        lifecycle_actions=AsyncMock(),
        reservation=RunReservationService(review=build_review_service()),
        incidents=LifecycleIncidentService(),
        session_factory=session_factory,
    )


@pytest.fixture(autouse=True)
def _stub_agent_reconfigure(monkeypatch: pytest.MonkeyPatch) -> None:
    # poke_node_refresh otherwise blocks waiting for a TCP connect to the test
    # host IP. Cooldown flows trigger this from inline delivery and from the
    # expired-intent reconciler sweep.
    monkeypatch.setattr(
        "app.agent_comm.node_poke.agent_operations.agent_nodes_refresh",
        AsyncMock(),
    )


async def _seed_node(db_session: AsyncSession, device_id: object) -> AppiumNode:
    node = AppiumNode(
        device_id=device_id,
        port=4723,
        desired_state=AppiumDesiredState.stopped,
    )
    db_session.add(node)
    await db_session.commit()
    return node


async def _reservation_for(db_session: AsyncSession, device_id: object) -> DeviceReservation:
    # cooldown_device now commits via its own session (session_factory-owned
    # transaction), so this fetch must bypass db_session's identity-map cache.
    result = await db_session.execute(
        select(DeviceReservation)
        .where(DeviceReservation.device_id == device_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def test_cooldown_counter_survives_intent_ttl_expiry(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """A cooldown TTL window lapses, then a fresh cooldown lands. The
    counter must accumulate to 2 — not reset to 1 — so the escalation
    threshold is reachable on slow-burn intermittent flakes."""
    device = await create_device(db_session, host_id=db_host.id, name="counter-persists")
    await _seed_node(db_session, device.id)
    run = await create_reserved_run(db_session, name="counter-persists-run", devices=[device])
    failure_svc = _make_failure_svc(db_session_maker)

    # First cooldown.
    result_1 = await failure_svc.cooldown_device(
        run.id,
        device.id,
        reason="probe timeout",
        ttl_seconds=60,
    )
    assert result_1.cooldown_count == 1
    assert not result_1.escalated
    assert result_1.excluded_until is not None
    threshold = result_1.threshold
    reservation = await _reservation_for(db_session, device.id)
    assert reservation.cooldown_count == 1
    assert reservation.excluded is True

    # Simulate the cooldown TTL elapsing by backdating the reservation row's
    # exclusion bounds, then run the intent reconciler pass that clears expired
    # timed exclusions. The reservation should be un-excluded, but
    # cooldown_count must stay sticky (cleared exclusion, not restore).
    past = datetime.now(UTC) - timedelta(seconds=1)
    earlier = datetime.now(UTC) - timedelta(seconds=120)
    # Backdate both bounds together — the computed ``excluded_window`` range
    # column requires excluded_at < excluded_until.
    reservation.excluded_at = earlier
    reservation.excluded_until = past
    await db_session.commit()

    await reconcile_device_command(
        db_session_maker,
        ReconcileCandidate(device.id, delete_expired_intents=False, clear_elapsed_cooldown=True),
        publisher=event_bus,
        packs={},
    )

    await db_session.refresh(reservation)
    assert reservation.excluded is False
    assert reservation.cooldown_count == 1, "counter must persist across exclusion clear"

    # Second cooldown lands after the first TTL elapsed. Counter accumulates.
    result_2 = await failure_svc.cooldown_device(
        run.id,
        device.id,
        reason="probe timeout again",
        ttl_seconds=60,
    )
    assert result_2.cooldown_count == 2, "second cooldown after TTL expiry must yield count=2"
    assert not result_2.escalated or threshold == 2

    await db_session.refresh(reservation)
    assert reservation.cooldown_count == 2


async def test_restore_device_to_run_resets_cooldown_counter(db_session: AsyncSession, db_host: Host) -> None:
    """Operator-driven restore is the sanctioned counter-reset point.

    Restore is the cleanup path for hard-excluded reservations (escalated
    cooldowns where ``excluded_until`` is null). When it runs, it must zero
    the cooldown counter so the next reservation cycle starts fresh.
    """
    device = await create_device(db_session, host_id=db_host.id, name="restore-resets")
    await _seed_node(db_session, device.id)
    await create_reserved_run(db_session, name="restore-resets-run", devices=[device])

    # Shape the entry like a post-escalation hard exclusion: cooldown_count
    # accumulated, ``excluded_until=None`` so the restore guard treats it as
    # still excluded and proceeds with the reset.
    reservation = await _reservation_for(db_session, device.id)
    reservation.excluded = True
    reservation.exclusion_reason = "escalated"
    reservation.excluded_at = datetime.now(UTC) - timedelta(minutes=5)
    reservation.excluded_until = None
    reservation.cooldown_count = 3
    await db_session.commit()

    await RunReservationService(review=build_review_service()).restore_device_to_run(db_session, device.id)
    await db_session.refresh(reservation)
    assert reservation.excluded is False
    assert reservation.cooldown_count == 0


async def test_expired_cooldown_preserves_counter(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """Regression: cooldown TTL clearing must not zero ``cooldown_count``.

    Symptom seen in production: operators set escalation threshold = 3, ran a
    long test run, devices accrued 5/6/11 cooldowns yet never escalated to
    maintenance. Each TTL window expired between cooldowns; the TTL-clear
    TTL-clear pass was finding those rows and resetting the
    counter back to 0, so ``cooldown_device``'s ``cooldown_count_after >=
    threshold`` check could never fire on subsequent increments.

    Design contract: the counter persists across exclusion clears. Only
    operator-driven ``restore_device_to_run`` resets it.
    """
    device = await create_device(db_session, host_id=db_host.id, name="legacy-sweep-no-reset")
    await _seed_node(db_session, device.id)
    await create_reserved_run(db_session, name="legacy-sweep-no-reset-run", devices=[device])

    reservation = await _reservation_for(db_session, device.id)
    excluded_at = datetime.now(UTC) - timedelta(seconds=120)
    reservation.excluded = True
    reservation.exclusion_kind = ExclusionKind.cooldown
    reservation.exclusion_reason = "flaky"
    reservation.excluded_at = excluded_at
    reservation.excluded_until = datetime.now(UTC) - timedelta(seconds=1)
    reservation.cooldown_count = 2
    await db_session.commit()

    await reconcile_device_command(
        db_session_maker,
        ReconcileCandidate(device.id, delete_expired_intents=False, clear_elapsed_cooldown=True),
        publisher=event_bus,
        packs={},
    )

    await db_session.refresh(reservation)
    assert reservation.excluded is False
    assert reservation.cooldown_count == 2, (
        "expired-cooldown pass must not zero the counter — escalation threshold must remain reachable across flakes"
    )
