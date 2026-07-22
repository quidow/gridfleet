"""Device intent reconciler: derives each device's desired state from intents and facts.

Ticks as the ``device_intent_reconciler`` background loop: inventories every
device, then clears its expired deny intents and elapsed cooldown under that
device's lock before reconciling it. Despite the name, unrelated to the observe-only
``appium_nodes.services.reconciler*`` family, which converges agent-reported
Appium process facts and never decides desired state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import delete, exists, select, update
from sqlalchemy.exc import NoResultFound

from app.agent_comm.node_poke import NodeRefreshTarget, poke_node_refresh_target
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.desired_state_writer import (
    DesiredStateWrite,
    write_desired_grid_run_id,
    write_desired_state,
)
from app.core import metrics_recorders
from app.core.background_loop import BackgroundLoop
from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import (
    Device,
    DeviceEventType,
    DeviceIntent,
    DeviceReservation,
    ExclusionKind,
)
from app.devices.services.claims import reservation_active
from app.devices.services.decision import (
    DecisionFacts,
    decide_grid_routing,
    decide_node_process,
    map_node_process_decision,
    parse_command,
)
from app.devices.services.event import record_event
from app.devices.services.intent_types import release_rollout_intent_source
from app.devices.services.readiness import load_packs_by_ids
from app.devices.services.state import WithdrawalFacts, emit_operational_state_transition
from app.lifecycle.services import remediation_log
from app.runs.models import RunState, TestRun
from app.sessions.live_session_predicate import device_has_live_session

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.type_defs import SessionFactory
    from app.devices.locking import LockedDevice
    from app.devices.services_container import DeviceServices
    from app.events.protocols import EventPublisher
    from app.lifecycle.services.remediation_log import LadderState
    from app.packs.models import DriverPack

logger = get_logger(__name__)
LOOP_NAME = "device_intent_reconciler"

# Plumbing constant (P5): the full-scan backstop cadence is not operator policy.
INTENT_RECONCILE_INTERVAL_SEC = 5.0


@dataclass(frozen=True, slots=True)
class ReconcileCandidate:
    device_id: uuid.UUID
    delete_expired_intents: bool = False
    clear_elapsed_cooldown: bool = False
    pack_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReconcileCommandResult:
    changed: bool
    target: NodeRefreshTarget | None


class DeviceIntentReconcilerLoop(BackgroundLoop):
    loop_name = LOOP_NAME
    cycle_failed_message = "device_intent_reconciler_cycle_failed"

    def __init__(self, *, services: DeviceServices) -> None:
        self._services = services

    @property
    def _session_factory(self) -> SessionFactory:
        return self._services.session_factory

    def _interval(self) -> float:
        return INTENT_RECONCILE_INTERVAL_SEC

    async def _run_cycle(self, db: AsyncSession) -> None:
        await run_device_intent_reconciler_once(
            db,
            session_factory=self._services.session_factory,
            circuit_breaker=self._services.circuit_breaker,
            publisher=self._services.publisher,
            pool=self._services.pool,
        )


async def reconcile_device_command(
    session_factory: SessionFactory,
    candidate: ReconcileCandidate,
    *,
    publisher: EventPublisher,
    packs: dict[str, DriverPack],
) -> ReconcileCommandResult:
    async with session_factory() as db, db.begin():
        try:
            locked = await device_locking.lock_device_handle(db, candidate.device_id)
        except NoResultFound:
            return ReconcileCommandResult(changed=False, target=None)
        await _apply_candidate_hygiene(db, locked, candidate=candidate, now=now_utc())
        changed = await reconcile_locked_device(db, locked, publisher=publisher, packs=packs)
        host = locked.device.host
        target = NodeRefreshTarget(host.ip, host.agent_port) if changed and host is not None else None
        return ReconcileCommandResult(changed=changed, target=target)


async def _reconcile_and_deliver(
    session_factory: SessionFactory,
    candidate: ReconcileCandidate,
    *,
    circuit_breaker: CircuitBreakerProtocol,
    publisher: EventPublisher,
    packs: dict[str, DriverPack],
    pool: AgentHttpPool | None = None,
) -> None:
    result = await reconcile_device_command(session_factory, candidate, publisher=publisher, packs=packs)
    if result.target is not None:
        await poke_node_refresh_target(result.target, circuit_breaker=circuit_breaker, pool=pool)


async def run_device_intent_reconciler_once(
    db: AsyncSession,
    *,
    session_factory: SessionFactory,
    circuit_breaker: CircuitBreakerProtocol,
    publisher: EventPublisher,
    pool: AgentHttpPool | None = None,
) -> None:
    async with db.begin():
        candidates = await _load_reconcile_candidates(db, now=now_utc())
        packs = await load_packs_by_ids(db, {candidate.pack_id for candidate in candidates if candidate.pack_id})
        for pack in packs.values():
            db.expunge(pack)

    for candidate in candidates:
        await _reconcile_and_deliver(
            session_factory,
            candidate,
            circuit_breaker=circuit_breaker,
            publisher=publisher,
            packs=packs,
            pool=pool,
        )


async def _load_reconcile_candidates(db: AsyncSession, *, now: datetime) -> list[ReconcileCandidate]:
    expired_intent = exists(
        select(DeviceIntent.id)
        .where(
            DeviceIntent.device_id == Device.id,
            DeviceIntent.expires_at.is_not(None),
            DeviceIntent.expires_at <= now,
        )
        .correlate(Device)
    )
    elapsed_cooldown = exists(
        select(DeviceReservation.id)
        .join(TestRun, TestRun.id == DeviceReservation.run_id)
        .where(
            DeviceReservation.device_id == Device.id,
            DeviceReservation.exclusion_kind == ExclusionKind.cooldown,
            DeviceReservation.excluded_until < now,
            reservation_active(),
            TestRun.state.notin_((RunState.completed, RunState.cancelled, RunState.failed)),
        )
        .correlate(Device)
    )
    rows = (
        await db.execute(
            select(
                Device.id,
                Device.pack_id,
                expired_intent.label("delete_expired_intents"),
                elapsed_cooldown.label("clear_elapsed_cooldown"),
            ).order_by(Device.id)
        )
    ).all()
    return [
        ReconcileCandidate(
            device_id=row.id,
            delete_expired_intents=bool(row.delete_expired_intents),
            clear_elapsed_cooldown=bool(row.clear_elapsed_cooldown),
            pack_id=row.pack_id,
        )
        for row in rows
    ]


async def _delete_expired_intents_for_locked_device(
    db: AsyncSession,
    locked: LockedDevice,
    *,
    now: datetime,
) -> None:
    locked.assert_active(db)
    await db.execute(
        delete(DeviceIntent).where(
            DeviceIntent.device_id == locked.device.id,
            DeviceIntent.expires_at.is_not(None),
            DeviceIntent.expires_at <= now,
        )
    )


async def _clear_elapsed_cooldown_for_locked_device(
    db: AsyncSession,
    locked: LockedDevice,
    *,
    now: datetime,
) -> None:
    locked.assert_active(db)
    nonterminal_run = exists(
        select(TestRun.id).where(
            TestRun.id == DeviceReservation.run_id,
            TestRun.state.notin_((RunState.completed, RunState.cancelled, RunState.failed)),
        )
    )
    await db.execute(
        update(DeviceReservation)
        .where(
            DeviceReservation.device_id == locked.device.id,
            DeviceReservation.exclusion_kind == ExclusionKind.cooldown,
            DeviceReservation.excluded_until < now,
            reservation_active(),
            nonterminal_run,
        )
        .values(
            excluded=False,
            exclusion_kind=None,
            exclusion_reason=None,
            excluded_at=None,
            excluded_until=None,
        )
    )


async def _apply_candidate_hygiene(
    db: AsyncSession,
    locked: LockedDevice,
    *,
    candidate: ReconcileCandidate,
    now: datetime,
) -> None:
    if candidate.delete_expired_intents:
        await _delete_expired_intents_for_locked_device(db, locked, now=now)
    if candidate.clear_elapsed_cooldown:
        try:
            async with db.begin_nested():
                await _clear_elapsed_cooldown_for_locked_device(db, locked, now=now)
        except Exception:
            logger.exception("intent_reconciler_cooldown_clear_failed", device_id=str(locked.device.id))


async def gather_decision_facts(
    db: AsyncSession, device: Device, now: datetime, *, ladder: LadderState | None = None
) -> DecisionFacts:
    """Facts the desired-state deciders fold in: reservation and remediation-log slice."""
    entry = (
        await db.execute(
            select(DeviceReservation)
            .where(DeviceReservation.device_id == device.id, reservation_active())
            .order_by(DeviceReservation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    reservation_run_id = None
    cooldown_active = False
    cooldown_reason: str | None = None
    if entry is not None and entry.exclusion_kind != ExclusionKind.exclusion:
        # An indefinite (health-failure) exclusion removes the device from run
        # routing entirely; a timed exclusion (cooldown) keeps the run bound
        # but blocks new sessions — both verbatim from the retired synthesis.
        reservation_run_id = entry.run_id
        if (
            entry.exclusion_kind == ExclusionKind.cooldown
            and entry.excluded_until is not None
            and entry.excluded_until > now
        ):
            cooldown_active = True
            cooldown_reason = entry.exclusion_reason
    withdrawal = WithdrawalFacts.from_device(device)
    if ladder is None:
        ladder = await remediation_log.load_ladder(db, device.id)
    return DecisionFacts(
        in_maintenance=withdrawal.in_maintenance,
        device_checks_unhealthy=device.device_checks_healthy is False,
        in_service=withdrawal.in_service(),
        reservation_run_id=reservation_run_id,
        cooldown_active=cooldown_active,
        cooldown_reason=cooldown_reason,
        remediation_directive=ladder.node_directive,
    )


async def reconcile_device(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    publisher: EventPublisher,
    packs: dict[str, DriverPack] | None = None,
) -> bool:
    """Re-derive desired node state and operational_state for one device.

    Returns True when agent-visible node state changed (desired state/port,
    grid run id, accepting_new_sessions, stop_pending) — the caller uses this
    to gate the agent wake poke. Derivations that only touch
    ``operational_state`` return False: the agent does not read it.
    """
    metrics_recorders.INTENT_RECONCILER_EVALUATIONS.inc()
    try:
        device = await device_locking.lock_device(db, device_id)
    except NoResultFound:
        # The device row was deleted concurrently (e.g. an operator delete
        # between the scan select and this lock). Nothing to reconcile —
        # skip without failing the whole reconcile cycle.
        return False
    return await _reconcile_loaded_device(db, device, publisher=publisher, packs=packs)


async def reconcile_locked_device(
    db: AsyncSession,
    locked: LockedDevice,
    *,
    publisher: EventPublisher,
    packs: dict[str, DriverPack] | None = None,
) -> bool:
    metrics_recorders.INTENT_RECONCILER_EVALUATIONS.inc()
    locked.assert_active(db)
    return await _reconcile_loaded_device(
        db,
        locked.device,
        publisher=publisher,
        packs=packs,
    )


async def _apply_rollout_stamp(
    db: AsyncSession,
    *,
    device_id: uuid.UUID,
    node: AppiumNode,
    stored: list[DeviceIntent],
    facts: DecisionFacts,
    now: datetime,
) -> list[DeviceIntent]:
    """Revoke a converged rollout intent inline (Finding 6) and mint the
    restart stamp once the rollout can safely apply (Findings 2, 5, 7).

    Returns the intent list with a converged rollout row removed so the
    decision ladder below does not see it.
    """
    rollout_source = release_rollout_intent_source(device_id)
    rollout_row = next((row for row in stored if row.source == rollout_source), None)
    target_release = rollout_row.payload.get("target_release") if rollout_row is not None else None
    # Finding 6: inline convergence revoke. The agent reports the converged
    # release within ~10 s of a restart (push fold into observed_pack_release);
    # drop the rollout intent here so the device re-enters the allocatable pool
    # without waiting for the 60 s janitor stage. The stage's revoke branch
    # remains the backstop for the no-longer-candidate cases.
    if rollout_row is not None and target_release is not None and node.observed_pack_release == target_release:
        await db.delete(rollout_row)
        return [row for row in stored if row is not rollout_row]
    # Stamp gate: mint restart_requested_at once the rollout can safely apply.
    # Finding 7: parse_command is the single liveness authority (expiry,
    # tombstone, unknown-kind) — do not re-derive its rules here. Finding 5:
    # only stamp while the node is still release-mismatched (a converged node
    # has no rollout to apply; a crash-restart that lands on the target
    # release must not be force-restarted). Finding 2: a reservation-bound
    # device is mid-run even between sessions — defer the stamp until the run
    # releases (mirrors pack drain's active-work definition). Finding 1: the
    # stamp is only the *intent* to restart; the watermark is re-validated
    # below at write time before it reaches the node.
    rollout_command = parse_command(rollout_row, now) if rollout_row is not None else None
    if (
        rollout_row is not None
        and rollout_command is not None
        and rollout_command.restart_requested_at is None
        and target_release is not None
        and node.observed_pack_release is not None
        and node.observed_pack_release != target_release
        and facts.reservation_run_id is None
        and not await device_has_live_session(db, device_id)
    ):
        rollout_row.payload = {**rollout_row.payload, "restart_requested_at": now.isoformat()}
    return stored


def _rollout_target_release(stored: list[DeviceIntent], device_id: uuid.UUID) -> str | None:
    """The target release of the live rollout intent for this device, or None."""
    rollout_source = release_rollout_intent_source(device_id)
    row = next((row for row in stored if row.source == rollout_source), None)
    if row is None:
        return None
    target = row.payload.get("target_release")
    return target if isinstance(target, str) else None


async def _reconcile_loaded_device(
    db: AsyncSession,
    device: Device,
    *,
    publisher: EventPublisher,
    packs: dict[str, DriverPack] | None = None,
) -> bool:
    device_id = device.id
    node = device.appium_node
    if node is None:
        # No Appium node — skip intent evaluation but still derive device state
        # so operational_state / hold stay consistent with durable facts.
        try:
            now = now_utc()
            await emit_operational_state_transition(db, device, now=now, publisher=publisher, packs=packs)
        except Exception:  # noqa: BLE001 - event emission is best-effort; a skipped edge retries next tick
            logger.warning("device-state derivation failed for %s (no node)", device_id, exc_info=True)
        return False

    now = now_utc()
    stored: list[DeviceIntent] = list(
        (
            await db.execute(
                select(DeviceIntent).where(DeviceIntent.device_id == device.id).order_by(DeviceIntent.source)
            )
        )
        .scalars()
        .all()
    )
    facts = await gather_decision_facts(db, device, now)
    stored = await _apply_rollout_stamp(db, device_id=device_id, node=node, stored=stored, facts=facts, now=now)
    target_release = _rollout_target_release(stored, device_id)
    commands = [c for c in (parse_command(row, now) for row in stored) if c is not None]

    node_decision = decide_node_process(commands, facts)
    grid_decision = decide_grid_routing(facts)
    target_state, node_accepting_new_sessions, stop_pending = map_node_process_decision(node_decision)
    # Findings 1, 5: re-validate at watermark-write time. A stamp minted when
    # the node was idle may sit dormant while a higher-ranked start wins the
    # ladder; by the time the rollout wins and carries the stamp, a session
    # may have started, the node may have converged, or a reservation may
    # have bound the device. Suppress the watermark in any of those cases —
    # the node is already draining (accepting_new_sessions=False), so the
    # stamp stays dormant and the next reconcile re-validates. Only the
    # rollout rung (running_draining) routes a stamp to the watermark; the
    # start rung carries its own watermark and is unaffected.
    restart_watermark = node_decision.restart_requested_at
    if (
        node_decision.desired_state == "running_draining"
        and restart_watermark is not None
        and (
            await device_has_live_session(db, device_id)
            or facts.reservation_run_id is not None
            or target_release is None
            or node.observed_pack_release == target_release
        )
    ):
        restart_watermark = None
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
        and await device_has_live_session(db, device_id)
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
        "restart_requested_at": node.restart_requested_at,
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
        caller="intent_reconciler",
        write=DesiredStateWrite(
            target=target_state,
            desired_port=desired_port,
            restart_requested_at=restart_watermark,
            reason=node_decision.reason,
        ),
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

    metadata_changed = (
        old["accepting_new_sessions"] != node.accepting_new_sessions
        or old["stop_pending"] != node.stop_pending
        or old["desired_grid_run_id"] != node.desired_grid_run_id
    )
    changed = metadata_changed or any(
        old[key] != getattr(node, key) for key in ("desired_state", "desired_port", "restart_requested_at")
    )
    await db.flush()

    try:
        await emit_operational_state_transition(db, device, now=now, publisher=publisher, packs=packs)
    except Exception:  # noqa: BLE001 - event emission is best-effort; a skipped edge retries next tick
        logger.warning("device-state derivation failed for %s", device_id, exc_info=True)

    return changed


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
