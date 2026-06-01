from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from prometheus_client import Counter
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.devices.models import Device, DeviceEventType, DeviceHold, DeviceOperationalState
from app.devices.models.intent import DeviceIntent
from app.devices.models.reservation import DeviceReservation
from app.devices.services.event import record_event
from app.devices.services.health_view import device_allows_allocation
from app.devices.services.intent_types import NODE_PROCESS, verification_intent_source
from app.devices.services.lifecycle_policy_state import state as policy_state
from app.devices.services.observation_reason import ObservationReason, map_transition_event
from app.devices.services.readiness import is_ready_for_use_async
from app.devices.services.state import appium_node_stop_in_flight, set_hold, set_operational_state
from app.sessions.models import Session, SessionStatus

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.events.protocols import EventPublisher

GATING_VIOLATION = Counter(
    "gridfleet_device_state_gating_violation_total",
    "An allocation landed on a device whose operational state forbade it (invariant breach).",
    ["kind"],
)


@dataclass(frozen=True)
class DeviceStateFacts:
    """All inputs the device-state derivation needs, pre-gathered (no IO here)."""

    has_running_session: bool  # a Session row status=running, ended_at IS NULL
    has_verification_lease: bool  # an active verification intent (§16 task 4)
    in_maintenance: bool  # lifecycle_policy_state["maintenance_reason"] set (§16.1)
    stop_in_flight: bool  # appium_node_stop_in_flight(device)
    ready: bool  # is_ready_for_use ∧ device_allows_allocation ∧ ¬review_required
    is_reserved: bool  # an active device_reservations row


def evaluate_operational_state(facts: DeviceStateFacts) -> DeviceOperationalState:
    """Derive the 5-value operational axis (spec §4): busy > verifying > maintenance > offline > available."""
    if facts.has_running_session:
        return DeviceOperationalState.busy
    if facts.has_verification_lease:
        return DeviceOperationalState.verifying
    if facts.in_maintenance:
        return DeviceOperationalState.maintenance
    if facts.stop_in_flight or not facts.ready:
        return DeviceOperationalState.offline
    return DeviceOperationalState.available


def evaluate_hold(facts: DeviceStateFacts) -> DeviceHold | None:
    """Derive the back-compat `hold` mirror. `maintenance` > `reserved` (§4)."""
    if facts.in_maintenance:
        return DeviceHold.maintenance
    if facts.is_reserved:
        return DeviceHold.reserved
    return None


async def gather_device_state_facts(db: AsyncSession, device: Device, *, now: datetime) -> DeviceStateFacts:
    """Gather all inputs needed for state derivation via async DB queries.

    ``device`` must be persistent (committed or flushed) in *db*.  The
    function refreshes ``device.appium_node`` eagerly so that
    ``device_allows_allocation`` and ``appium_node_stop_in_flight`` can
    inspect the node without triggering synchronous lazy loading.
    """
    # Reload the device with appium_node eager-loaded so health-view helpers
    # can access it synchronously without triggering MissingGreenlet.
    device = (
        await db.execute(select(Device).where(Device.id == device.id).options(selectinload(Device.appium_node)))
    ).scalar_one()

    has_running_session = (
        await db.execute(
            select(Session.id)
            .where(
                Session.device_id == device.id,
                Session.status == SessionStatus.running,
                Session.ended_at.is_(None),
            )
            .limit(1)
        )
    ).first() is not None

    has_verification_lease = (
        await db.execute(
            select(DeviceIntent.id)
            .where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.axis == NODE_PROCESS,
                DeviceIntent.source == verification_intent_source(device.id),
                or_(DeviceIntent.expires_at.is_(None), DeviceIntent.expires_at > now),
            )
            .limit(1)
        )
    ).first() is not None

    is_reserved = (
        await db.execute(
            select(DeviceReservation.id)
            .where(
                DeviceReservation.device_id == device.id,
                DeviceReservation.released_at.is_(None),
            )
            .limit(1)
        )
    ).first() is not None

    in_maintenance = policy_state(device).get("maintenance_reason") is not None
    ready = await is_ready_for_use_async(db, device) and device_allows_allocation(device) and not device.review_required

    return DeviceStateFacts(
        has_running_session=has_running_session,
        has_verification_lease=has_verification_lease,
        in_maintenance=in_maintenance,
        stop_in_flight=appium_node_stop_in_flight(device),
        ready=ready,
        is_reserved=is_reserved,
    )


def _reason_for(
    prev_op: DeviceOperationalState,
    facts: DeviceStateFacts,
    derived_op: DeviceOperationalState,
) -> ObservationReason:
    """Map gathered facts to the closest ObservationReason for the derived transition.

    Priority mirrors evaluate_operational_state: session > verification_lease >
    stop_in_flight / not_ready, then split the ``available`` destination by where
    it came from: leaving ``busy`` is a session end, anything else recovering to
    ``available`` is a recovery (so ``offline → available`` maps to
    ``connectivity_restored``, not ``session_ended``).
    """
    if facts.has_running_session:
        return ObservationReason.session
    if facts.has_verification_lease:
        return ObservationReason.verification_started
    if facts.stop_in_flight:
        return ObservationReason.auto_stopped
    if not facts.ready:
        return ObservationReason.disconnected
    if prev_op is DeviceOperationalState.busy:
        return ObservationReason.session_ended
    return ObservationReason.recovered


async def apply_derived_state(
    db: AsyncSession,
    device: Device,
    *,
    now: datetime,
    publisher: EventPublisher | None = None,
    observed_reason: ObservationReason | None = None,
) -> bool:
    """Derive (operational_state, hold) and write them when they differ from the persisted columns.

    Emits the mapped operational/hold bus event for each axis that changes when ``publisher`` is
    provided.

    The typed DeviceEvent audit row is written only when the caller carries an ``observed_reason``
    (§6: the cause rides on the observation, the reconciler maps ``(delta, reason) → event``). The
    reconciler must NOT *guess* the cause from facts alone — a not-ready offline transition could be
    a connectivity loss, a node crash, a health-probe failure or a verification failure, and each
    has a different DeviceEventType. Mislabelling them all ``connectivity_lost`` would corrupt the
    analytics reliability counts that query that type. Background reconciles (no carried reason)
    still update state and emit the bus event, but record no audit row.

    Returns True if either axis was written.
    """
    facts = await gather_device_state_facts(db, device, now=now)
    derived_op = evaluate_operational_state(facts)
    derived_hold = evaluate_hold(facts)

    changed = False

    if derived_op is not device.operational_state:
        prev_op = device.operational_state
        reason = observed_reason if observed_reason is not None else _reason_for(prev_op, facts, derived_op)
        event_type, severity = map_transition_event(derived_op, reason)
        # Persist the typed audit row the old EventLogHook used to write (§6) only when the cause was
        # explicitly carried in by the observation site — never from the fact-based heuristic, which
        # cannot tell connectivity loss from a node crash / health failure.
        if observed_reason is not None and event_type is not None:
            await record_event(
                db,
                device.id,
                event_type,
                {"from": prev_op.value, "to": derived_op.value, "reason": reason.value},
            )
        await set_operational_state(
            device,
            derived_op,
            reason=(event_type.value if event_type is not None else reason.value),
            severity=severity,
            publisher=publisher,
        )
        changed = True

    if derived_hold is not device.hold:
        prev_hold = device.hold
        # Maintenance enter/exit are the only hold transitions the old EventLogHook audited
        # (reserved hold churn from allocation/release recorded no row).
        if derived_hold is DeviceHold.maintenance:
            await record_event(db, device.id, DeviceEventType.maintenance_entered, {"reason": "maintenance"})
            hold_reason = "maintenance"
        elif prev_hold is DeviceHold.maintenance:
            await record_event(db, device.id, DeviceEventType.maintenance_exited, {"reason": "exit maintenance"})
            hold_reason = "exit maintenance"
        else:
            hold_reason = derived_hold.value if derived_hold is not None else "released"
        await set_hold(device, derived_hold, reason=hold_reason, severity="info", publisher=publisher)
        changed = True

    return changed
