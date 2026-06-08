"""Writers + helpers for the device operational-state axis.

operational_state -- what the device is doing (available/busy/verifying/maintenance/offline).

Events are queued through the SQLAlchemy session so they fire on commit, not
before. Bypassing the queue causes ghost transitions when the surrounding
transaction rolls back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from prometheus_client import Counter
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.appium_nodes.models import AppiumDesiredState
from app.core.observability import get_logger
from app.devices.models import Device, DeviceEventType, DeviceOperationalState
from app.devices.models.intent import DeviceIntent
from app.devices.services.event import record_event
from app.devices.services.health_view import device_allows_allocation
from app.devices.services.intent_types import NODE_PROCESS, verification_intent_source
from app.devices.services.lifecycle_policy_state import state as policy_state
from app.devices.services.observation_reason import ObservationReason, map_transition_event
from app.devices.services.readiness import is_ready_for_use_async
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session as OrmSession

    from app.events.catalog import EventSeverity
    from app.events.protocols import EventPublisher
    from app.packs.models import DriverPack

logger = get_logger(__name__)


def _persistent_session(device: Device) -> OrmSession:
    state = sa_inspect(device, raiseerr=False)
    assert state is not None and state.persistent, (
        "Device must be persistent in a session; callers that write state "
        "must load it through lock_device in the same transaction"
    )
    session = state.session
    assert session is not None, "device has no session despite persistent==True"
    return session


async def set_operational_state(
    device: Device,
    new_state: DeviceOperationalState,
    *,
    reason: str | None = None,
    publish_event: bool = True,
    severity: EventSeverity | None = None,
    publisher: EventPublisher,
) -> bool:
    session = _persistent_session(device)
    old = device.operational_state
    if old == new_state:
        return False
    device.operational_state = new_state
    if publish_event:
        payload = {
            "device_id": str(device.id),
            "device_name": device.name,
            "old_operational_state": old.value,
            "new_operational_state": new_state.value,
        }
        if reason is not None:
            payload["reason"] = reason
        publisher.queue_for_session(
            session,
            "device.operational_state_changed",
            payload,
            severity=severity,
        )
    return True


def appium_node_stop_in_flight(device: Device) -> bool:
    """Return True when a stop intent has been written to the device's Appium
    node row but the agent has not yet finished tearing the relay down.

    The reconciler may write ``desired_state=stopped`` or ``stop_pending=True``
    well before the agent observes the change and disconnects the relay from
    the Selenium hub. During that window the node row still looks
    operational (``pid``, ``active_connection_target`` populated), so any
    caller that gates on ``operational_state == available`` alone could hand
    the device to a new run only to have the session removed as soon as the
    relay deregisters. Callers must consult this predicate alongside the
    operational axis.

    Lazy-load safety: if ``appium_node`` is not eager-loaded, return False
    rather than trigger a sync IO under an AsyncSession (which raises
    ``MissingGreenlet``). Critical gating call sites — ``service_sync``,
    ``verification_execution`` — already eager-load via
    ``device_locking.lock_device``; non-eager call sites get a conservative
    answer that matches the pre-existing behavior.
    """
    if "appium_node" in sa_inspect(device).unloaded:
        return False
    node = device.appium_node
    if node is None:
        return False
    return node.desired_state == AppiumDesiredState.stopped or bool(node.stop_pending)


async def ready_operational_state(db: AsyncSession, device: Device) -> DeviceOperationalState:
    """Project readiness into the operational axis."""
    if device.operational_state is DeviceOperationalState.verifying:
        return DeviceOperationalState.verifying
    if appium_node_stop_in_flight(device):
        # Intent-driven stops (graceful health-failure deferral, cooldown,
        # maintenance) must not surface as ``available``. The relay is on
        # its way out; the device is offline-bound until the intent clears.
        return DeviceOperationalState.offline
    if await is_ready_for_use_async(db, device) and device_allows_allocation(device):
        return DeviceOperationalState.available
    return DeviceOperationalState.offline


# --- derived-state evaluation (formerly state_derivation.py) ---

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


def device_in_service(device: Device) -> bool:
    """Eligibility gate for ``baseline:idle`` node starts (F-G1).

    A device withdrawn from service must never receive a baseline-started
    node. Withdrawal facts only (``verified_at``, ``maintenance_reason``,
    ``review_required``) — deliberately NOT the full ``ready`` fact:
    ``device_allows_allocation`` inspects node health, so using full
    readiness here would deadlock a stopped node against ever
    baseline-starting. Keep in lockstep with the withdrawal facts in
    ``gather_device_state_facts``.
    """
    return (
        device.verified_at is not None
        and policy_state(device).get("maintenance_reason") is None
        and not device.review_required
    )


async def gather_device_state_facts(
    db: AsyncSession, device: Device, *, now: datetime, packs: dict[str, DriverPack] | None = None
) -> DeviceStateFacts:
    """Gather all inputs needed for state derivation via async DB queries.

    ``device`` must be persistent (committed or flushed) in *db*.  The
    function refreshes ``device.appium_node`` eagerly so that
    ``device_allows_allocation`` and ``appium_node_stop_in_flight`` can
    inspect the node without triggering synchronous lazy loading.
    """
    # Reload the device with appium_node eager-loaded so health-view helpers
    # can access it synchronously without triggering MissingGreenlet. Skip the
    # reload when appium_node is already loaded (the reconciler path always
    # passes a lock_device-loaded, row-locked device) — re-selecting it would
    # just re-run two queries and return the same in-session object.
    if "appium_node" in sa_inspect(device).unloaded:
        device = (
            await db.execute(select(Device).where(Device.id == device.id).options(selectinload(Device.appium_node)))
        ).scalar_one()

    has_running_session = (
        await db.execute(select(Session.id).where(live_session_predicate(device.id)).limit(1))
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

    in_maintenance = policy_state(device).get("maintenance_reason") is not None
    ready = (
        await is_ready_for_use_async(db, device, packs=packs)
        and device_allows_allocation(device)
        and not device.review_required
    )

    return DeviceStateFacts(
        has_running_session=has_running_session,
        has_verification_lease=has_verification_lease,
        in_maintenance=in_maintenance,
        stop_in_flight=appium_node_stop_in_flight(device),
        ready=ready,
    )


def _reason_for(
    prev_op: DeviceOperationalState,
    facts: DeviceStateFacts,
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
    if facts.in_maintenance:
        # Mirror evaluate_operational_state's priority: maintenance outranks
        # stop_in_flight / not-ready. Entering maintenance registers a node-stop
        # intent (stop_in_flight=True) and may leave the device not-ready, so
        # without this branch the bus event would report ``auto_stopped`` /
        # ``disconnected`` — contradicting the ``maintenance_entered`` audit row.
        return ObservationReason.maintenance_entered
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
    publisher: EventPublisher,
    observed_reason: ObservationReason | None = None,
    packs: dict[str, DriverPack] | None = None,
) -> bool:
    """Derive ``operational_state`` and write it when it differs from the persisted column.

    Emits the mapped operational bus event when the axis changes and ``publisher`` is provided.

    The typed DeviceEvent audit row is written only when the caller carries an ``observed_reason``
    (§6: the cause rides on the observation, the reconciler maps ``(delta, reason) → event``). The
    reconciler must NOT *guess* the cause from facts alone — a not-ready offline transition could be
    a connectivity loss, a node crash, a health-probe failure or a verification failure, and each
    has a different DeviceEventType. Mislabelling them all ``connectivity_lost`` would corrupt the
    analytics reliability counts that query that type. Background reconciles (no carried reason)
    still update state and emit the bus event, but record no audit row.

    Maintenance enter/exit transitions on the operational axis are structurally unambiguous, so they
    always record a ``maintenance_entered`` / ``maintenance_exited`` audit row regardless of a carried
    reason.

    Returns True if the operational axis was written.
    """
    facts = await gather_device_state_facts(db, device, now=now, packs=packs)
    derived_op = evaluate_operational_state(facts)

    if derived_op is device.operational_state:
        return False

    prev_op = device.operational_state
    reason = observed_reason if observed_reason is not None else _reason_for(prev_op, facts)
    event_type, severity = map_transition_event(derived_op, reason)
    # Persist the typed audit row when the cause was
    # explicitly carried in by the observation site — never from the fact-based heuristic, which
    # cannot tell connectivity loss from a node crash / health failure.
    if observed_reason is not None and event_type is not None:
        await record_event(
            db,
            device.id,
            event_type,
            {"from": prev_op.value, "to": derived_op.value, "reason": reason.value},
        )
    # Maintenance enter/exit are structurally unambiguous (the maintenance operational state has
    # exactly one cause) so the audit row is always recorded, mirroring the old hold-axis behavior.
    if derived_op is DeviceOperationalState.maintenance:
        await record_event(db, device.id, DeviceEventType.maintenance_entered, {"reason": "maintenance"})
    elif prev_op is DeviceOperationalState.maintenance:
        await record_event(db, device.id, DeviceEventType.maintenance_exited, {"reason": "exit maintenance"})
    await set_operational_state(
        device,
        derived_op,
        reason=(event_type.value if event_type is not None else reason.value),
        severity=severity,
        publisher=publisher,
    )
    return True
