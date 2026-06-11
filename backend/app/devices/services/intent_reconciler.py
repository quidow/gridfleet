from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import cast, delete, func, or_, select
from sqlalchemy.dialects.postgresql import UUID

from app.agent_comm.models import AgentReconfigureOutbox
from app.agent_comm.reconfigure_delivery import deliver_agent_reconfigures, deliver_pending_agent_reconfigures
from app.appium_nodes.models import AppiumDesiredState
from app.appium_nodes.services.desired_state_writer import write_desired_grid_run_id, write_desired_state
from app.core import metrics_recorders
from app.core.background_loop import BackgroundLoop
from app.core.leader.advisory import assert_current_leader
from app.core.observability import get_logger
from app.devices import locking as device_locking
from app.devices.models import (
    Device,
    DeviceEventType,
    DeviceIntent,
    DeviceIntentDirty,
    DeviceOperationalState,
    DeviceReservation,
)
from app.devices.services.event import record_event
from app.devices.services.intent_evaluator import (
    ReservationDecision,
    evaluate_grid_routing,
    evaluate_node_process,
    evaluate_recovery,
    evaluate_reservation,
    map_node_process_decision,
)
from app.devices.services.intent_types import GRID_ROUTING, NODE_PROCESS, PRIORITY_IDLE, RECOVERY, RESERVATION
from app.devices.services.readiness import load_packs_by_ids
from app.devices.services.state import apply_derived_state, device_in_service
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.appium_nodes.models import AppiumNode
    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory
    from app.devices.services.observation_reason import ObservationReason
    from app.devices.services_container import DeviceServices
    from app.events.protocols import EventPublisher
    from app.packs.models import DriverPack

logger = get_logger(__name__)
LOOP_NAME = "device_intent_reconciler"


class DeviceIntentReconcilerLoop(BackgroundLoop):
    loop_name = LOOP_NAME
    exit_on_leadership_lost = True
    cycle_failed_message = "device_intent_reconciler_cycle_failed"

    def __init__(self, *, services: DeviceServices) -> None:
        self._services = services
        self._cycle = 0

    @property
    def _session_factory(self) -> SessionFactory:
        return self._services.session_factory

    def _leadership_lost_event(self) -> str:
        return "device_intent_reconciler_leadership_lost"  # historical name: no "_loop" segment

    def _interval(self) -> float:
        return float(self._services.settings.get_int("general.intent_reconcile_interval_sec"))

    async def _run_cycle(self, db: AsyncSession) -> None:
        await run_device_intent_reconciler_once(
            db,
            cycle=self._cycle,
            settings=self._services.settings,
            circuit_breaker=self._services.circuit_breaker,
            publisher=self._services.publisher,
            pool=self._services.pool,
        )

    def _on_cycle_end(self, elapsed_seconds: float, interval: float) -> None:
        self._cycle += 1


async def run_device_intent_reconciler_once(
    db: AsyncSession,
    *,
    cycle: int,
    settings: SettingsReader,
    circuit_breaker: CircuitBreakerProtocol,
    publisher: EventPublisher,
    pool: AgentHttpPool | None = None,
) -> None:
    await assert_current_leader(db, settings=settings)
    full_scan_every = settings.get_int("general.intent_reconcile_full_scan_every_cycles")
    await deliver_pending_agent_reconfigures(
        db, settings=settings, circuit_breaker=circuit_breaker, publisher=publisher, pool=pool
    )
    await _reconcile_expired_intents(
        db, settings=settings, circuit_breaker=circuit_breaker, publisher=publisher, pool=pool
    )
    try:
        await _sweep_orphaned_intents(db)
    except Exception:
        await db.rollback()
        logger.exception("stale_intent_sweep_failed")
    await _reconcile_terminal_run_intents(
        db, settings=settings, circuit_breaker=circuit_breaker, publisher=publisher, pool=pool
    )
    from app.devices.services.intent_evaluator import (  # noqa: PLC0415
        reconcile_unsatisfied_preconditions,
    )

    precondition_affected = await reconcile_unsatisfied_preconditions(db)
    for affected_id in sorted(precondition_affected):
        await reconcile_device(db, affected_id, publisher=publisher)
        await db.commit()
        await deliver_agent_reconfigures(
            db, affected_id, settings=settings, circuit_breaker=circuit_breaker, publisher=publisher, pool=pool
        )
    if cycle % full_scan_every == 0:
        await _reconcile_all_devices_once(
            db, settings=settings, circuit_breaker=circuit_breaker, publisher=publisher, pool=pool
        )
    else:
        await _reconcile_dirty_devices(
            db, settings=settings, circuit_breaker=circuit_breaker, publisher=publisher, pool=pool
        )


async def _reconcile_all_devices_once(
    db: AsyncSession,
    *,
    settings: SettingsReader,
    circuit_breaker: CircuitBreakerProtocol,
    publisher: EventPublisher,
    pool: AgentHttpPool | None = None,
) -> None:
    intent_device_ids = (await db.execute(select(DeviceIntent.device_id).distinct())).scalars().all()
    # The reconciler is authoritative for operational_state, but a device with no
    # intents would otherwise be invisible to this scan — so a state pushed without
    # its backing intent (e.g. a bare `verifying`/`offline` push stranded by a crash
    # before the lease was registered; spec §14.5: every state must be backed by a
    # durable fact) could stick forever. Also re-derive any device not in the steady
    # `available` state so orphaned non-available states always self-heal. Steady
    # `available` devices with no intents stay skipped (the original optimization).
    orphan_device_ids = (
        (await db.execute(select(Device.id).where(Device.operational_state != DeviceOperationalState.available)))
        .scalars()
        .all()
    )
    scan_ids = list(dict.fromkeys([*intent_device_ids, *orphan_device_ids]))
    # Prefetch the pack catalog once for the whole scan so each reconcile_device skips
    # its per-device pack load (see _reconcile_dirty_devices for the same pattern).
    packs: dict[str, DriverPack] = {}
    if scan_ids:
        pack_ids = (await db.execute(select(Device.pack_id).where(Device.id.in_(scan_ids)))).scalars().all()
        packs = await load_packs_by_ids(db, {pid for pid in pack_ids if pid})
    for device_id in scan_ids:
        await reconcile_device(db, device_id, publisher=publisher, packs=packs)
        await db.commit()
        await deliver_agent_reconfigures(
            db, device_id, settings=settings, circuit_breaker=circuit_breaker, publisher=publisher, pool=pool
        )


async def _reconcile_dirty_devices(
    db: AsyncSession,
    *,
    settings: SettingsReader,
    limit: int = 100,
    circuit_breaker: CircuitBreakerProtocol,
    publisher: EventPublisher,
    pool: AgentHttpPool | None = None,
) -> None:
    queue_size = await db.scalar(select(func.count()).select_from(DeviceIntentDirty))
    metrics_recorders.INTENT_RECONCILER_DIRTY_QUEUE_SIZE.set(int(queue_size or 0))
    rows = (
        (await db.execute(select(DeviceIntentDirty).order_by(DeviceIntentDirty.dirty_at).limit(limit))).scalars().all()
    )
    # Prefetch the driver-pack catalog once for the whole dirty batch so each
    # reconcile_device skips its own per-device pack load (readiness check).
    # expire_on_commit=False keeps these objects usable across the per-device commits below.
    device_ids = [row.device_id for row in rows]
    packs: dict[str, DriverPack] = {}
    if device_ids:
        pack_ids = (await db.execute(select(Device.pack_id).where(Device.id.in_(device_ids)))).scalars().all()
        packs = await load_packs_by_ids(db, {pid for pid in pack_ids if pid})
    for row in rows:
        device_id = row.device_id
        generation = row.generation
        await reconcile_device(db, device_id, publisher=publisher, packs=packs)
        current = await db.get(DeviceIntentDirty, device_id, populate_existing=True)
        if current is not None and current.generation == generation:
            await db.delete(current)
        await db.commit()
        await deliver_agent_reconfigures(
            db, device_id, settings=settings, circuit_breaker=circuit_breaker, publisher=publisher, pool=pool
        )


async def _reconcile_expired_intents(
    db: AsyncSession,
    *,
    settings: SettingsReader,
    circuit_breaker: CircuitBreakerProtocol,
    publisher: EventPublisher,
    pool: AgentHttpPool | None = None,
) -> None:
    now = datetime.now(UTC)
    device_ids = (
        (
            await db.execute(
                select(DeviceIntent.device_id).where(
                    DeviceIntent.expires_at.is_not(None), DeviceIntent.expires_at <= now
                )
            )
        )
        .scalars()
        .all()
    )
    if not device_ids:
        return
    await db.execute(delete(DeviceIntent).where(DeviceIntent.expires_at.is_not(None), DeviceIntent.expires_at <= now))
    for device_id in sorted(set(device_ids)):
        await reconcile_device(db, device_id, publisher=publisher)
        await db.commit()
        await deliver_agent_reconfigures(
            db, device_id, settings=settings, circuit_breaker=circuit_breaker, publisher=publisher, pool=pool
        )


async def _sweep_orphaned_intents(db: AsyncSession) -> None:
    """Revoke orphaned ``DeviceIntent`` rows.

    Defense in depth: producer modules own the primary revoke paths. This sweep
    catches any branch that skips its revoke obligation. Counters increment per
    revoked row, labeled by intent source family.
    """
    # 1. active_session:{sid} — Session.ended_at IS NOT NULL.
    active_session_ids = (
        (
            await db.execute(
                select(DeviceIntent.id)
                .where(DeviceIntent.source.like("active_session:%"))
                .join(
                    Session,
                    Session.session_id == func.substring(DeviceIntent.source, len("active_session:") + 1),
                )
                .where(Session.ended_at.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    if active_session_ids:
        await db.execute(delete(DeviceIntent).where(DeviceIntent.id.in_(active_session_ids)))
        metrics_recorders.STALE_INTENT_SWEEP_REVOKED.labels(source="active_session").inc(len(active_session_ids))

    # 2. connectivity:{device_id} — device not offline AND device_checks_healthy IS NOT FALSE.
    connectivity_ids = (
        (
            await db.execute(
                select(DeviceIntent.id)
                .where(DeviceIntent.source.like("connectivity:%"))
                .join(Device, Device.id == DeviceIntent.device_id)
                .where(
                    Device.operational_state != DeviceOperationalState.offline,
                    or_(Device.device_checks_healthy.is_(None), Device.device_checks_healthy.is_(True)),
                )
            )
        )
        .scalars()
        .all()
    )
    if connectivity_ids:
        await db.execute(delete(DeviceIntent).where(DeviceIntent.id.in_(connectivity_ids)))
        metrics_recorders.STALE_INTENT_SWEEP_REVOKED.labels(source="connectivity").inc(len(connectivity_ids))

    # 3. cooldown:{axis}:{run_id} — DeviceReservation.released_at IS NOT NULL.
    # Source format: "cooldown:<axis>:<run_uuid>". Split on ':' and cast last segment.
    cooldown_rows = (
        await db.execute(
            select(DeviceIntent.id, DeviceIntent.source)
            .where(DeviceIntent.source.like("cooldown:%"))
            .join(
                DeviceReservation,
                # Postgres split_part is 1-indexed; segment 3 is the run_id.
                DeviceReservation.run_id == cast(func.split_part(DeviceIntent.source, ":", 3), UUID(as_uuid=True)),
            )
            .where(DeviceReservation.released_at.is_not(None))
        )
    ).all()
    if cooldown_rows:
        ids = [row.id for row in cooldown_rows]
        await db.execute(delete(DeviceIntent).where(DeviceIntent.id.in_(ids)))
        for row in cooldown_rows:
            axis = row.source.split(":")[1]
            metrics_recorders.STALE_INTENT_SWEEP_REVOKED.labels(source=f"cooldown:{axis}").inc()

    await db.flush()


async def _reconcile_terminal_run_intents(
    db: AsyncSession,
    *,
    settings: SettingsReader,
    circuit_breaker: CircuitBreakerProtocol,
    publisher: EventPublisher,
    pool: AgentHttpPool | None = None,
) -> None:
    """Defense-in-depth sweep for intents tied to runs that are already terminal.

    The release path (``_clear_desired_grid_run_id_for_run``) is the primary
    cleanup for run-scoped intents. This sweep guards against any release path
    skipping a source or crashing mid-release — a run-bound intent must never
    outlive its owning run.
    """
    from app.runs.models import TERMINAL_STATES, TestRun  # noqa: PLC0415

    terminal_run_subq = select(TestRun.id).where(TestRun.state.in_(TERMINAL_STATES))
    device_ids = (
        (
            await db.execute(
                select(DeviceIntent.device_id)
                .where(DeviceIntent.run_id.is_not(None))
                .where(DeviceIntent.run_id.in_(terminal_run_subq))
            )
        )
        .scalars()
        .all()
    )
    if not device_ids:
        return
    await db.execute(
        delete(DeviceIntent).where(DeviceIntent.run_id.is_not(None)).where(DeviceIntent.run_id.in_(terminal_run_subq))
    )
    for device_id in sorted(set(device_ids)):
        await reconcile_device(db, device_id, publisher=publisher)
        await db.commit()
        await deliver_agent_reconfigures(
            db, device_id, settings=settings, circuit_breaker=circuit_breaker, publisher=publisher, pool=pool
        )


async def reconcile_device(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    publisher: EventPublisher,
    observed_reason: ObservationReason | None = None,
    packs: dict[str, DriverPack] | None = None,
) -> None:
    metrics_recorders.INTENT_RECONCILER_EVALUATIONS.inc()
    device = await device_locking.lock_device(db, device_id)
    node = device.appium_node
    if node is None:
        # No Appium node — skip intent evaluation but still derive device state
        # so operational_state / hold stay consistent with durable facts.
        try:
            now = datetime.now(UTC)
            await apply_derived_state(
                db, device, now=now, publisher=publisher, observed_reason=observed_reason, packs=packs
            )
        except Exception:  # noqa: BLE001 - state derivation must never break reconcile
            logger.warning("device-state derivation failed for %s (no node)", device_id, exc_info=True)
        return

    now = datetime.now(UTC)
    intents = (
        (
            await db.execute(
                select(DeviceIntent).where(DeviceIntent.device_id == device_id).order_by(DeviceIntent.source)
            )
        )
        .scalars()
        .all()
    )
    intent_count = await db.scalar(select(func.count()).select_from(DeviceIntent))
    metrics_recorders.INTENT_REGISTRY_INTENTS.set(int(intent_count or 0))
    active_node_intents = [
        intent
        for intent in intents
        if intent.axis == NODE_PROCESS and (intent.expires_at is None or intent.expires_at > now)
    ]
    if not active_node_intents and device_in_service(device):
        intents = [
            *intents,
            DeviceIntent(
                device_id=device_id,
                source="baseline:idle",
                axis=NODE_PROCESS,
                payload={"action": "start", "priority": PRIORITY_IDLE, "desired_port": node.port},
            ),
        ]

    node_decision = evaluate_node_process([intent for intent in intents if intent.axis == NODE_PROCESS], now)
    grid_decision = evaluate_grid_routing([intent for intent in intents if intent.axis == GRID_ROUTING], now)
    reservation_decision = evaluate_reservation([intent for intent in intents if intent.axis == RESERVATION], now)
    recovery_decision = evaluate_recovery([intent for intent in intents if intent.axis == RECOVERY], now)
    target_state, node_accepting_new_sessions, stop_pending = map_node_process_decision(node_decision)
    # Universal session-safety invariant: only an explicit hard stop
    # (``stop_mode == "hard"`` — operator force-release, bulk operator stop,
    # same-priority conflict) may flip ``desired_state=stopped`` while a
    # client session is active. Graceful stops AND the no-intent stop (a
    # withdrawn device whose baseline was suppressed, F-G1) defer: the node
    # keeps running with ``accepting_new_sessions=False`` until the session
    # ends, then the next reconcile executes the stop.
    if (
        target_state == AppiumDesiredState.stopped
        and node_decision.stop_mode in (None, "graceful")
        and await _device_has_active_client_session(db, device_id)
    ):
        target_state = AppiumDesiredState.running
        node_accepting_new_sessions = False
        stop_pending = True
    accepting_new_sessions = node_accepting_new_sessions and grid_decision.accepting_new_sessions

    old = {
        "desired_state": node.desired_state,
        "desired_port": node.desired_port,
        "desired_grid_run_id": node.desired_grid_run_id,
        "accepting_new_sessions": node.accepting_new_sessions,
        "stop_pending": node.stop_pending,
        "recovery_allowed": device.recovery_allowed,
        "recovery_blocked_reason": device.recovery_blocked_reason,
    }

    # The node row is the single source of port truth: payload `desired_port` values are
    # registration-time snapshots of node.port and go stale when a fallback start moves the
    # node (observation updates node.port and clears AppiumNode.desired_port). Re-applying
    # a stale snapshot flips desired_port against the live port on every reconcile, and the
    # appium reconciler then force-restarts the node onto the stale port — the 4724<->4725
    # churn storm behind the N11 S13/S14 failures (2026-06-07). Pin the live port instead.
    desired_port = node.port if target_state == AppiumDesiredState.running else None
    await write_desired_state(
        db,
        node=node,
        target=target_state,
        desired_port=desired_port,
        transition_token=node_decision.transition_token,
        transition_deadline=node_decision.transition_deadline,
        caller="intent_reconciler",
        reason=node_decision.reason,
    )
    await write_desired_grid_run_id(
        db,
        node=node,
        run_id=grid_decision.run_id,
        caller="intent_reconciler",
        reason=grid_decision.reason,
    )

    if node.accepting_new_sessions != accepting_new_sessions:
        await _record_field_change(
            db,
            device_id,
            "accepting_new_sessions",
            node.accepting_new_sessions,
            accepting_new_sessions,
            grid_decision.reason,
        )
        node.accepting_new_sessions = accepting_new_sessions
    if node.stop_pending != stop_pending:
        await _record_field_change(db, device_id, "stop_pending", node.stop_pending, stop_pending, node_decision.reason)
        node.stop_pending = stop_pending

    if device.recovery_allowed != recovery_decision.allowed:
        await _record_field_change(
            db,
            device_id,
            "recovery_allowed",
            device.recovery_allowed,
            recovery_decision.allowed,
            recovery_decision.reason,
        )
        device.recovery_allowed = recovery_decision.allowed
    if device.recovery_blocked_reason != recovery_decision.reason:
        await _record_field_change(
            db,
            device_id,
            "recovery_blocked_reason",
            device.recovery_blocked_reason,
            recovery_decision.reason,
            recovery_decision.reason,
        )
        device.recovery_blocked_reason = recovery_decision.reason

    await _apply_reservation_decision(db, device_id, reservation_decision)

    metadata_changed = (
        old["accepting_new_sessions"] != node.accepting_new_sessions
        or old["stop_pending"] != node.stop_pending
        or old["desired_grid_run_id"] != node.desired_grid_run_id
    )
    changed = metadata_changed or any(
        old[key] != getattr(node if key.startswith("desired") else device, key)
        for key in ("desired_state", "desired_port", "recovery_allowed", "recovery_blocked_reason")
    )
    if changed:
        node.generation += 1
    # Stage a reconfigure for caps/run-id updates on a running node, and for
    # any transition that must stop new sessions reaching it: ``stop_pending``
    # (graceful) or ``not accepting_new_sessions`` (a hard stop flips this
    # without setting ``stop_pending``). Without the ``accepting_new_sessions``
    # arm, a hard/idle ``desired_state=stopped`` derives ``offline`` synchronously
    # (``stop_in_flight``) but pushes no drain — the Appium node stays UP and a
    # direct session lands on the now-offline device (N7,
    # ``session_on_non_available``). Mirrors the start-path defense-in-depth in
    # ``reconciler_agent.mark_node_running``.
    should_stage_reconfigure = (
        metadata_changed
        and node.port is not None
        and (node.desired_state == AppiumDesiredState.running or node.stop_pending or not node.accepting_new_sessions)
    )
    if should_stage_reconfigure:
        await _stage_agent_reconfigure(db, node)
    await db.flush()

    try:
        await apply_derived_state(
            db, device, now=now, publisher=publisher, observed_reason=observed_reason, packs=packs
        )
    except Exception:  # noqa: BLE001 - state derivation must never break reconcile
        logger.warning("device-state derivation failed for %s", device_id, exc_info=True)


async def _device_has_active_client_session(db: AsyncSession, device_id: uuid.UUID) -> bool:
    # ``pending`` is the allocate->confirm window (a placeholder session row exists
    # before the Appium id is confirmed). A graceful stop must defer for it the same
    # as for ``running``, or the Appium process is killed mid-create and the client
    # gets "session not created". Shared via live_session_predicate.
    count = await db.scalar(select(func.count()).select_from(Session).where(live_session_predicate(device_id)))
    return bool(count)


async def _apply_reservation_decision(db: AsyncSession, device_id: uuid.UUID, decision: ReservationDecision) -> None:
    reservation = (
        await db.execute(
            select(DeviceReservation)
            .where(DeviceReservation.device_id == device_id, DeviceReservation.released_at.is_(None))
            .order_by(DeviceReservation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if reservation is None:
        return
    if decision.excluded:
        await _update_reservation_exclusion(db, reservation, decision)
    else:
        await _clear_reservation_exclusion(db, reservation, decision.reason)


async def _update_reservation_exclusion(
    db: AsyncSession,
    reservation: DeviceReservation,
    decision: ReservationDecision,
) -> None:
    # The reservation row is the authority on cooldown_count; the intent
    # payload only mirrors the value that was current when the intent was
    # registered. Take the max so a stale or lower payload (e.g. a
    # non-cooldown reservation intent like ``health_failure:reservation``)
    # never walks the counter backwards.
    next_cooldown_count = max(reservation.cooldown_count, decision.cooldown_count or 0)
    changed = (
        reservation.excluded is not True
        or reservation.exclusion_reason != decision.exclusion_reason
        or reservation.excluded_until != decision.expires_at
        or reservation.cooldown_count != next_cooldown_count
    )
    if not changed:
        return
    old = {
        "excluded": reservation.excluded,
        "exclusion_reason": reservation.exclusion_reason,
        "excluded_until": reservation.excluded_until.isoformat() if reservation.excluded_until else None,
        "cooldown_count": reservation.cooldown_count,
    }
    reservation.excluded = True
    reservation.exclusion_reason = decision.exclusion_reason
    reservation.excluded_until = decision.expires_at
    reservation.cooldown_count = next_cooldown_count
    if reservation.excluded_at is None:
        reservation.excluded_at = datetime.now(UTC)
    await record_event(
        db,
        reservation.device_id,
        DeviceEventType.desired_state_changed,
        {
            "field": "reservation_exclusion",
            "old_value": old,
            "new_value": {
                "excluded": reservation.excluded,
                "exclusion_reason": reservation.exclusion_reason,
                "excluded_until": reservation.excluded_until.isoformat() if reservation.excluded_until else None,
                "cooldown_count": reservation.cooldown_count,
            },
            "caller": "intent_reconciler",
            "reason": decision.reason,
        },
    )


async def _clear_reservation_exclusion(db: AsyncSession, reservation: DeviceReservation, reason: str) -> None:
    if not reservation.excluded and reservation.exclusion_reason is None and reservation.excluded_until is None:
        return
    old = {
        "excluded": reservation.excluded,
        "exclusion_reason": reservation.exclusion_reason,
        "excluded_until": reservation.excluded_until.isoformat() if reservation.excluded_until else None,
        "cooldown_count": reservation.cooldown_count,
    }
    reservation.excluded = False
    reservation.exclusion_reason = None
    reservation.excluded_until = None
    # ``cooldown_count`` deliberately persists across exclusion clear/reset. It
    # tracks "how many cooldowns has this reservation seen" for the duration
    # of the reservation, so the escalation threshold is reachable even when
    # the cooldown TTL keeps lapsing between flakes. The counter is zeroed
    # only when the reservation is released or explicitly restored.
    await record_event(
        db,
        reservation.device_id,
        DeviceEventType.desired_state_changed,
        {
            "field": "reservation_exclusion",
            "old_value": old,
            "new_value": {
                "excluded": False,
                "exclusion_reason": None,
                "excluded_until": None,
                "cooldown_count": reservation.cooldown_count,
            },
            "caller": "intent_reconciler",
            "reason": reason,
        },
    )


async def _stage_agent_reconfigure(db: AsyncSession, node: AppiumNode) -> None:
    existing = (
        await db.execute(
            select(AgentReconfigureOutbox.id)
            .where(
                AgentReconfigureOutbox.device_id == node.device_id,
                AgentReconfigureOutbox.delivered_at.is_(None),
                AgentReconfigureOutbox.abandoned_at.is_(None),
                AgentReconfigureOutbox.reconciled_generation == node.generation,
                AgentReconfigureOutbox.port == node.port,
                AgentReconfigureOutbox.accepting_new_sessions == node.accepting_new_sessions,
                AgentReconfigureOutbox.stop_pending == node.stop_pending,
                AgentReconfigureOutbox.grid_run_id == node.desired_grid_run_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    db.add(
        AgentReconfigureOutbox(
            device_id=node.device_id,
            port=node.port,
            accepting_new_sessions=node.accepting_new_sessions,
            stop_pending=node.stop_pending,
            grid_run_id=node.desired_grid_run_id,
            reconciled_generation=node.generation,
        )
    )


async def _record_field_change(
    db: AsyncSession,
    device_id: uuid.UUID,
    field: str,
    old_value: object,
    new_value: object,
    reason: str | None,
) -> None:
    await record_event(
        db,
        device_id,
        DeviceEventType.desired_state_changed,
        {
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "caller": "intent_reconciler",
            "reason": reason,
        },
    )
