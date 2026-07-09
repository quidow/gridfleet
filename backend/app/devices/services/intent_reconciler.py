from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.exc import NoResultFound

from app.agent_comm.node_poke import poke_node_refresh
from app.appium_nodes.models import AppiumDesiredState
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
)
from app.devices.services.decision import (
    DecisionFacts,
    decide_grid_routing,
    decide_node_process,
    map_node_process_decision,
    parse_command,
)
from app.devices.services.event import record_event
from app.devices.services.lifecycle_policy_state import in_maintenance
from app.devices.services.readiness import load_packs_by_ids
from app.devices.services.state import apply_derived_state, device_in_service
from app.sessions.live_session_predicate import device_has_live_session

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
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
    cycle_failed_message = "device_intent_reconciler_cycle_failed"

    def __init__(self, *, services: DeviceServices) -> None:
        self._services = services

    @property
    def _session_factory(self) -> SessionFactory:
        return self._services.session_factory

    def _interval(self) -> float:
        return float(self._services.settings.get_int("general.intent_reconcile_interval_sec"))

    async def _run_cycle(self, db: AsyncSession) -> None:
        await run_device_intent_reconciler_once(
            db,
            settings=self._services.settings,
            circuit_breaker=self._services.circuit_breaker,
            publisher=self._services.publisher,
            pool=self._services.pool,
        )


async def _reconcile_commit_deliver(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    settings: SettingsReader,
    circuit_breaker: CircuitBreakerProtocol,
    publisher: EventPublisher,
    pool: AgentHttpPool | None = None,
    packs: dict[str, DriverPack] | None = None,
) -> None:
    if packs is not None:
        changed = await reconcile_device(db, device_id, publisher=publisher, packs=packs)
    else:
        changed = await reconcile_device(db, device_id, publisher=publisher)
    await db.commit()
    if changed:
        await poke_node_refresh(
            db, device_id, settings=settings, circuit_breaker=circuit_breaker, publisher=publisher, pool=pool
        )


async def run_device_intent_reconciler_once(
    db: AsyncSession,
    *,
    settings: SettingsReader,
    circuit_breaker: CircuitBreakerProtocol,
    publisher: EventPublisher,
    pool: AgentHttpPool | None = None,
) -> None:
    await _gc_expired_intents(db)
    await _reconcile_all_devices(db, settings=settings, circuit_breaker=circuit_breaker, publisher=publisher, pool=pool)


async def _gc_expired_intents(db: AsyncSession) -> None:
    """Bulk-delete expired intent rows. Pure hygiene: the evaluator already
    ignores rows past ``expires_at``, and this same tick's full scan
    re-derives every device — no per-device reconcile is needed here."""
    now = now_utc()
    await db.execute(delete(DeviceIntent).where(DeviceIntent.expires_at.is_not(None), DeviceIntent.expires_at <= now))
    await db.commit()


async def _reconcile_all_devices(
    db: AsyncSession,
    *,
    settings: SettingsReader,
    circuit_breaker: CircuitBreakerProtocol,
    publisher: EventPublisher,
    pool: AgentHttpPool | None = None,
) -> None:
    # ponytail: full scan every tick — no dirty queue, no work-avoidance. At lab
    # scale (hundreds of devices, ~8 short indexed queries each) a scan is cheap,
    # and it structurally removes the missed-mark_dirty staleness class. If a
    # very large lab ever needs relief, raise general.intent_reconcile_interval_sec.
    device_ids = (await db.execute(select(Device.id).order_by(Device.id))).scalars().all()
    packs: dict[str, DriverPack] = {}
    if device_ids:
        pack_ids = (await db.execute(select(Device.pack_id).distinct())).scalars().all()
        packs = await load_packs_by_ids(db, {pid for pid in pack_ids if pid})
    for device_id in device_ids:
        await _reconcile_commit_deliver(
            db,
            device_id,
            settings=settings,
            circuit_breaker=circuit_breaker,
            publisher=publisher,
            pool=pool,
            packs=packs,
        )


async def gather_decision_facts(db: AsyncSession, device: Device, now: datetime) -> DecisionFacts:
    """Facts the desired-state deciders fold in. One query (reservation); the
    rest reads the already-locked device row."""
    entry = (
        await db.execute(
            select(DeviceReservation)
            .where(DeviceReservation.device_id == device.id, DeviceReservation.released_at.is_(None))
            .order_by(DeviceReservation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    reservation_run_id = None
    cooldown_active = False
    cooldown_reason: str | None = None
    if entry is not None and not (entry.excluded and entry.excluded_until is None):
        # An indefinite (health-failure) exclusion removes the device from run
        # routing entirely; a timed exclusion (cooldown) keeps the run bound
        # but blocks new sessions — both verbatim from the retired synthesis.
        reservation_run_id = entry.run_id
        if entry.excluded and entry.excluded_until is not None and entry.excluded_until > now:
            cooldown_active = True
            cooldown_reason = entry.exclusion_reason
    return DecisionFacts(
        in_maintenance=in_maintenance(device),
        device_checks_unhealthy=device.device_checks_healthy is False,
        in_service=device_in_service(device),
        reservation_run_id=reservation_run_id,
        cooldown_active=cooldown_active,
        cooldown_reason=cooldown_reason,
    )


async def reconcile_device(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    publisher: EventPublisher,
    observed_reason: ObservationReason | None = None,
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
    node = device.appium_node
    if node is None:
        # No Appium node — skip intent evaluation but still derive device state
        # so operational_state / hold stay consistent with durable facts.
        try:
            now = now_utc()
            await apply_derived_state(
                db, device, now=now, publisher=publisher, observed_reason=observed_reason, packs=packs
            )
        except Exception:  # noqa: BLE001 - state derivation must never break reconcile
            logger.warning("device-state derivation failed for %s (no node)", device_id, exc_info=True)
        return False

    now = now_utc()
    stored = (
        (
            await db.execute(
                select(DeviceIntent).where(DeviceIntent.device_id == device.id).order_by(DeviceIntent.source)
            )
        )
        .scalars()
        .all()
    )
    commands = [c for c in (parse_command(row, now) for row in stored) if c is not None]
    facts = await gather_decision_facts(db, device, now)

    node_decision = decide_node_process(commands, facts)
    grid_decision = decide_grid_routing(facts)
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
            restart_requested_at=node_decision.restart_requested_at,
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
        await apply_derived_state(
            db, device, now=now, publisher=publisher, observed_reason=observed_reason, packs=packs
        )
    except Exception:  # noqa: BLE001 - state derivation must never break reconcile
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
