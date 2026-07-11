from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import ANY, AsyncMock, Mock, patch

import pytest
from sqlalchemy import select

from app.agent_comm.node_poke import poke_node_refresh
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceIntent, DeviceOperationalState, DeviceReservation
from app.devices.services.intent import IntentService
from app.devices.services.intent_reconciler import (
    _gc_expired_intents,
    _reconcile_commit_deliver,
    reconcile_device,
    run_device_intent_reconciler_once,
)
from app.devices.services.intent_types import CommandKind, IntentRegistration
from app.devices.services.lifecycle_policy_state import set_maintenance_reason
from app.devices.services.state import derive_operational_state
from app.sessions.models import Session, SessionStatus
from tests.fakes import FakeSettingsReader
from tests.fakes.review import build_review_service
from tests.helpers import create_device, create_reserved_run
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


async def _seed_node(db_session: AsyncSession, device_id: object, *, generation: int = 0) -> AppiumNode:
    _ = generation
    node = AppiumNode(
        device_id=device_id,
        port=4723,
        desired_state=AppiumDesiredState.stopped,
    )
    db_session.add(node)
    await db_session.commit()
    return node


async def test_baseline_eligible_device_derives_running(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="baseline")
    await _seed_node(db_session, device.id)

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    node = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
    assert node.desired_state == AppiumDesiredState.running
    assert node.accepting_new_sessions is True


async def test_reconcile_uses_facts_directly_for_maintenance(db_session: AsyncSession, db_host: Host) -> None:
    """Maintenance must stop the node with NO intent row present — the fact is
    read directly, not synthesized into a transient intent."""
    device = await create_device(db_session, host_id=db_host.id, name="maint")
    await _seed_node(db_session, device.id)

    locked = await device_locking.lock_device(db_session, device.id)
    set_maintenance_reason(locked, "operator hold")
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    node = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
    assert node.desired_state == AppiumDesiredState.stopped
    assert node.stop_pending is True  # graceful: sets the stop-pending flag via map
    intent_rows = (await db_session.execute(select(DeviceIntent))).scalars().all()
    assert intent_rows == []  # no synthesized or stored rows involved


async def test_review_required_device_gets_no_baseline_node(db_session: AsyncSession, db_host: Host) -> None:
    """F-G1: a shelved device (review_required) must not be baseline-started.

    Live finding 2026-06-05: after an update-mode verify failure shelved a
    device, baseline:idle kept desired_state=running and the hub slot stayed
    UP/free for >=180s (S10/G3).
    """
    device = await create_device(db_session, host_id=db_host.id, name="shelved")
    device.review_required = True
    await db_session.commit()
    node = await _seed_node(db_session, device.id)

    await reconcile_device(db_session, device.id, publisher=event_bus)

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped
    assert node.accepting_new_sessions is False


async def test_cooldown_intents_derive_metadata_reservation_and_recovery(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="cooldown")
    await _seed_node(db_session, device.id)
    run = await create_reserved_run(db_session, name="cooldown run", devices=[device])
    reservation = (
        await db_session.execute(select(DeviceReservation).where(DeviceReservation.device_id == device.id))
    ).scalar_one()
    reservation.excluded = True
    reservation.exclusion_reason = "Device in cooldown"
    reservation.excluded_at = datetime.now(UTC)
    reservation.excluded_until = datetime.now(UTC) + timedelta(minutes=5)
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    node = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
    await db_session.refresh(reservation)
    assert node.desired_state == AppiumDesiredState.running
    assert node.accepting_new_sessions is False
    assert node.desired_grid_run_id == run.id
    assert reservation.excluded is True


async def test_expired_intents_are_deleted_and_reconciled(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="expired")
    await _seed_node(db_session, device.id)
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"operator:start:{device.id}",
                kind=CommandKind.operator_start,
                payload={"action": "start", "priority": 90},
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            ),
        ],
    )
    await db_session.commit()

    await run_device_intent_reconciler_once(
        db_session, settings=FakeSettingsReader(), circuit_breaker=Mock(), publisher=event_bus
    )

    intents = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    node = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
    assert intents == []
    assert node.accepting_new_sessions is True


async def test_graceful_stop_stages_agent_drain_before_convergence_can_stop(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="graceful")
    node = await _seed_node(db_session, device.id, generation=2)
    node.desired_state = AppiumDesiredState.running
    node.desired_port = 4723
    node.port = 4723
    node.pid = 1234
    node.active_connection_target = device.connection_target
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"health_failure:node:{device.id}",
                kind=CommandKind.health_failure_stop,
                payload={"action": "stop", "stop_mode": "graceful"},
            ),
        ],
    )
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped
    assert node.stop_pending is True
    assert node.accepting_new_sessions is False


async def test_hard_stop_on_idle_device_stages_agent_drain(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A hard stop of an idle device must stage a drain reconfigure (N7).

    ``operational_state`` derives to ``offline`` synchronously from
    ``desired_state=stopped`` (``stop_in_flight``), but the relay keeps running
    and registered to the hub until the appium reconciler tears it down. Without
    a drain pushed to the agent, the hub keeps routing and a direct/free session
    lands on the now-offline device → ``busy`` (the ``session_on_non_available``
    gating violation). The hard-stop path must stage the same drain the graceful
    path does, even though ``desired_state=stopped`` and ``stop_pending=False``.
    """
    device = await create_device(db_session, host_id=db_host.id, name="hard-stop")
    node = await _seed_node(db_session, device.id, generation=2)
    node.desired_state = AppiumDesiredState.running
    node.desired_port = 4723
    node.port = 4723
    node.pid = 1234
    node.active_connection_target = device.connection_target
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"operator:stop:node:{device.id}",
                kind=CommandKind.operator_stop,
                payload={"action": "stop", "stop_mode": "hard"},
            ),
        ],
    )
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped
    assert node.stop_pending is False
    assert node.accepting_new_sessions is False


async def test_graceful_stop_holds_node_running_while_session_active(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A graceful stop intent must not flip ``desired_state=stopped`` while a
    client session is running on the device.

    The convergence layer's ``stop_pending`` no-op currently guards the kill,
    but it is the only safety net. This test pins the contract that the
    intent reconciler itself holds the soft-stop while a session is active —
    so any caller (heartbeat-driven crash, future code path, evaluator change)
    that forgets to set ``stop_pending`` cannot terminate the relay mid-session.
    """
    device = await create_device(db_session, host_id=db_host.id, name="graceful-session")
    node = await _seed_node(db_session, device.id, generation=2)
    node.desired_state = AppiumDesiredState.running
    node.desired_port = 4723
    node.port = 4723
    node.pid = 1234
    node.active_connection_target = device.connection_target
    db_session.add(Session(session_id="active-sess-1", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"health_failure:node:{device.id}",
                kind=CommandKind.health_failure_stop,
                payload={"action": "stop", "stop_mode": "graceful", "priority": 60},
            ),
        ],
    )
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running
    assert node.stop_pending is True
    assert node.accepting_new_sessions is False


async def test_graceful_stop_holds_node_running_while_session_pending(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A ``pending`` session (the allocate->confirm window) must defer a graceful stop
    the same way a ``running`` session does (F1). Killing the Appium process mid-create
    fails the in-flight session creation.
    """
    device = await create_device(db_session, host_id=db_host.id, name="graceful-pending")
    node = await _seed_node(db_session, device.id, generation=2)
    node.desired_state = AppiumDesiredState.running
    node.desired_port = 4723
    node.port = 4723
    node.pid = 1234
    node.active_connection_target = device.connection_target
    db_session.add(Session(session_id="alloc-pending-1", device_id=device.id, status=SessionStatus.pending))
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"health_failure:node:{device.id}",
                kind=CommandKind.health_failure_stop,
                payload={"action": "stop", "stop_mode": "graceful", "priority": 60},
            ),
        ],
    )
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running
    assert node.stop_pending is True
    assert node.accepting_new_sessions is False


async def test_graceful_stop_applies_once_session_ends(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """After the held session ends, the next reconcile must apply the held
    graceful stop and flip ``desired_state=stopped`` with ``stop_pending=True``.
    """
    device = await create_device(db_session, host_id=db_host.id, name="graceful-end")
    node = await _seed_node(db_session, device.id, generation=2)
    node.desired_state = AppiumDesiredState.running
    node.desired_port = 4723
    node.port = 4723
    node.pid = 1234
    node.active_connection_target = device.connection_target
    session = Session(session_id="ending-sess-1", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"health_failure:node:{device.id}",
                kind=CommandKind.health_failure_stop,
                payload={"action": "stop", "stop_mode": "graceful", "priority": 60},
            ),
        ],
    )
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()
    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running

    session.status = SessionStatus.passed
    session.ended_at = datetime.now(UTC)
    await db_session.commit()
    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped
    assert node.stop_pending is True
    assert node.accepting_new_sessions is False


async def test_pull_host_metadata_change_pokes_instead_of_staging(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A metadata-only desired-state change must wake the agent with a
    fire-and-forget poke instead of staging any delivery row."""
    device = await create_device(db_session, host_id=db_host.id, name="pull-metadata")
    node = await _seed_node(db_session, device.id, generation=7)
    node.desired_state = AppiumDesiredState.running
    node.desired_port = 4723
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"operator:start:{device.id}",
                kind=CommandKind.operator_start,
                payload={"action": "start", "priority": 20},
            ),
        ],
    )
    await db_session.commit()
    poke = AsyncMock()
    monkeypatch.setattr("app.agent_comm.node_poke.agent_operations.agent_nodes_refresh", poke)

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()
    await poke_node_refresh(
        db_session, device.id, settings=FakeSettingsReader(), circuit_breaker=Mock(), publisher=event_bus
    )

    poke.assert_awaited_once_with(db_host.ip, db_host.agent_port, pool=None, circuit_breaker=ANY)


async def test_pull_host_watermark_only_change_pokes_agent(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="pull-watermark")
    node = await _seed_node(db_session, device.id)
    node.desired_state = AppiumDesiredState.running
    node.desired_port = 4723
    await db_session.commit()
    requested_at = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)
    await IntentService(db_session).register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"auto_recovery:node:{device.id}",
                kind=CommandKind.auto_recovery_start,
                payload={"action": "start", "restart_requested_at": requested_at.isoformat()},
            )
        ],
    )
    await db_session.commit()
    poke = AsyncMock()
    monkeypatch.setattr("app.agent_comm.node_poke.agent_operations.agent_nodes_refresh", poke)

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()
    await poke_node_refresh(
        db_session, device.id, settings=FakeSettingsReader(), circuit_breaker=Mock(), publisher=event_bus
    )

    await db_session.refresh(node)
    assert node.restart_requested_at == requested_at
    poke.assert_awaited_once_with(db_host.ip, db_host.agent_port, pool=None, circuit_breaker=ANY)


async def test_scan_rederives_stale_available_device_without_intents(
    db_session: AsyncSession, db_host: Host, seeded_driver_packs: None
) -> None:
    """The case the old dirty queue existed for: a fact changes on a steady
    `available` device with no intent rows and nobody calls an inline
    reconcile. The every-tick scan must re-derive it."""
    device = await create_device(db_session, host_id=db_host.id, name="scan-rederive")
    node = await _seed_node(db_session, device.id)
    node.pid = 1
    node.active_connection_target = "target"
    node.health_running = True
    device.device_checks_healthy = True
    await db_session.commit()
    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()
    await db_session.refresh(device)
    assert await derive_operational_state(db_session, device, now=now_utc()) is not DeviceOperationalState.offline

    device.device_checks_healthy = False  # simulate an unswept fact flip
    await db_session.commit()

    await run_device_intent_reconciler_once(
        db_session,
        settings=FakeSettingsReader(),
        circuit_breaker=Mock(),
        publisher=event_bus,
    )
    refreshed = await db_session.get(Device, device.id)
    assert refreshed is not None
    assert await derive_operational_state(db_session, refreshed, now=now_utc()) is DeviceOperationalState.offline


async def test_gc_expired_intents_deletes_rows_only(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="gc-expired")
    await IntentService(db_session).register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"operator:start:{device.id}",
                kind=CommandKind.operator_start,
                payload={"action": "start", "priority": 20},
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        ],
    )
    await db_session.commit()

    await _gc_expired_intents(db_session)
    remaining = (await db_session.execute(select(DeviceIntent))).scalars().all()
    assert remaining == []


async def test_maintenance_signal_suppresses_baseline_idle_injection(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Baseline:idle must NOT be injected when the maintenance_reason signal is set,
    even if hold is NULL (i.e., the derived hold column has not yet been written)."""
    from app.devices.services.lifecycle_policy_state import set_maintenance_reason

    device = await create_device(db_session, host_id=db_host.id, name="maintenance-idle")
    node = await _seed_node(db_session, device.id)

    # Set maintenance_reason signal directly; leave device.hold as NULL.
    set_maintenance_reason(device, "test maintenance")
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    await db_session.refresh(node)
    # Node must remain stopped — no baseline:idle should have been injected.
    assert node.desired_state == AppiumDesiredState.stopped


@pytest.mark.parametrize(
    ("verified", "maintenance", "review"),
    [
        (False, False, False),  # unverified
        (True, True, False),  # maintenance
        (True, False, True),  # review-shelved (F-G1)
        (True, True, True),  # both withdrawal flags
    ],
)
async def test_withdrawn_device_never_gets_baseline_node(
    db_session: AsyncSession, db_host: Host, verified: bool, maintenance: bool, review: bool
) -> None:
    """Invariant: withdrawn-from-service => no baseline-started node.

    Drift-guard for device_in_service — if a new withdrawal fact is added to
    state derivation without updating the predicate, extend this matrix.
    """
    device = await create_device(db_session, host_id=db_host.id, name="withdrawn")
    if not verified:
        device.verified_at = None
    device.review_required = review
    if maintenance:
        device.lifecycle_policy_state = {**(device.lifecycle_policy_state or {}), "maintenance_reason": "operator"}
    await db_session.commit()
    node = await _seed_node(db_session, device.id)

    await reconcile_device(db_session, device.id, publisher=event_bus)

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped
    assert node.accepting_new_sessions is False


async def test_recovery_promoted_review_device_gets_no_baseline_node(db_session: AsyncSession, db_host: Host) -> None:
    """A device promoted to review_required by session_viability (recovery
    threshold) must drop out of baseline node starts — pre-fix, suppressed
    recovery left no intents and baseline:idle kept restarting the node."""
    device = await create_device(db_session, host_id=db_host.id, name="promoted")
    node = await _seed_node(db_session, device.id)

    review = build_review_service()
    marked = await review.mark_review_required(
        db_session, device, reason="Recovery probe failed", source="session_viability"
    )
    assert marked is True
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped


async def test_no_intent_stop_holds_node_running_while_session_active(db_session: AsyncSession, db_host: Host) -> None:
    """Only an explicit stop_mode='hard' may flip desired_state=stopped while
    a client session is active. The no-intent stop (withdrawn device, F-G1
    gate) must defer like a graceful stop."""
    device = await create_device(db_session, host_id=db_host.id, name="busy-shelved")
    device.review_required = True
    await db_session.commit()
    node = await _seed_node(db_session, device.id)
    node.desired_state = AppiumDesiredState.running
    db_session.add(Session(session_id="s-1", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running
    assert node.accepting_new_sessions is False
    assert node.stop_pending is True


async def test_start_intent_stale_payload_port_is_overridden_by_live_node_port(
    db_session: AsyncSession, db_host: Host
) -> None:
    """N11 churn co-defect (2026-06-07): a persistent start intent pins desired_port in its
    payload at registration time. A fallback start later moves the node (observation updates
    node.port and clears desired_port), after which re-applying the snapshot flips desired_port
    against the live port on every reconcile (the 4724<->4725 storm) and convergence
    force-restarts the node onto the stale port. The node row is the single source of port
    truth — the applier must pin live node.port, not the payload snapshot."""
    device = await create_device(db_session, host_id=db_host.id, name="port-pin", verified=True)
    node = await _seed_node(db_session, device.id)
    node.port = 4725  # fallback start moved the node here; payload below predates the move
    await db_session.commit()
    await IntentService(db_session).register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"operator:start:{device.id}",
                kind=CommandKind.operator_start,
                payload={"action": "start", "priority": 20, "desired_port": 4723},
            )
        ],
    )

    await reconcile_device(db_session, device.id, publisher=event_bus)

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running
    assert node.desired_port == 4725, (
        f"applier must pin live node.port over the stale payload snapshot; got {node.desired_port}"
    )


async def test_full_scan_corrects_drifted_offline_device_end_to_end(
    db_session: AsyncSession,
    db_host: Host,
    seeded_driver_packs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Safety net for transition-gating: a device with healthy facts whose
    operational_state drifted to offline (no dirty row, no observation transition)
    is re-derived off offline by the authoritative full scan."""
    device = await create_device(db_session, host_id=db_host.id, name="drifted")
    await _seed_node(db_session, device.id)
    # Make the node observed-running and the device-checks healthy so the reconciler
    # derives a non-offline state.
    node = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
    node.pid = 1
    node.active_connection_target = "target"
    node.health_running = True
    device.device_checks_healthy = True
    device.operational_state_last_emitted = DeviceOperationalState.offline  # the drift
    await db_session.commit()

    monkeypatch.setattr("app.devices.services.intent_reconciler.poke_node_refresh", AsyncMock())

    await run_device_intent_reconciler_once(
        db_session, settings=FakeSettingsReader(), circuit_breaker=Mock(), publisher=event_bus
    )
    await db_session.refresh(device)
    assert device.operational_state_last_emitted != DeviceOperationalState.offline  # drift corrected


async def test_steady_state_reconcile_does_not_poke(db_session: AsyncSession, db_host: Host) -> None:
    """A reconcile that changes nothing must not wake the agent."""
    device = await create_device(db_session, host_id=db_host.id, name="steady-state")
    await _seed_node(db_session, device.id)
    settings = FakeSettingsReader()
    # First reconcile settles initial derivation; the second is steady-state.
    await _reconcile_commit_deliver(
        db_session, device.id, settings=settings, circuit_breaker=Mock(), publisher=event_bus
    )
    poke = AsyncMock()
    with patch("app.devices.services.intent_reconciler.poke_node_refresh", poke):
        await _reconcile_commit_deliver(
            db_session, device.id, settings=settings, circuit_breaker=Mock(), publisher=event_bus
        )
    poke.assert_not_awaited()


async def test_desired_state_change_pokes_agent(db_session: AsyncSession, db_host: Host) -> None:
    """A reconcile that flips desired node state must wake the agent."""
    device = await create_device(db_session, host_id=db_host.id, name="pokes-agent")
    await _seed_node(db_session, device.id)

    poke = AsyncMock()
    with patch("app.devices.services.intent_reconciler.poke_node_refresh", poke):
        await _reconcile_commit_deliver(
            db_session, device.id, settings=FakeSettingsReader(), circuit_breaker=Mock(), publisher=event_bus
        )
    poke.assert_awaited_once()
