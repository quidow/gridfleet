"""Writers + helpers for the device operational-state axis.

operational_state -- what the device is doing (available/busy/verifying/maintenance/offline).

Events are queued through the SQLAlchemy session so they fire on commit, not
before. Bypassing the queue causes ghost transitions when the surrounding
transaction rolls back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.appium_nodes.models import AppiumDesiredState
from app.core.observability import get_logger
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.claims import device_has_live_session, device_has_verification_lease
from app.devices.services.health_view import device_allows_allocation
from app.devices.services.lifecycle_policy_state import in_maintenance
from app.devices.services.readiness import is_ready_for_use_async

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


def _transition_severity(old: DeviceOperationalState, new: DeviceOperationalState) -> EventSeverity:
    """Severity of the operational bus event, derived from the transition alone.

    Going offline warrants operator attention; recovering to available (from anything but a
    session end) is good news; everything else is routine.
    """
    if new is DeviceOperationalState.offline:
        return "warning"
    if new is DeviceOperationalState.available and old is not DeviceOperationalState.busy:
        return "success"
    return "info"


async def set_operational_state(
    device: Device,
    new_state: DeviceOperationalState,
    *,
    publish_event: bool = True,
    publisher: EventPublisher,
) -> bool:
    session = _persistent_session(device)
    old = device.operational_state
    if old == new_state:
        return False
    device.operational_state = new_state
    if publish_event:
        publisher.queue_for_session(
            session,
            "device.operational_state_changed",
            {
                "device_id": str(device.id),
                "device_name": device.name,
                "old_operational_state": old.value,
                "new_operational_state": new_state.value,
            },
            severity=_transition_severity(old, new_state),
        )
    return True


def appium_node_stop_in_flight(device: Device) -> bool:
    """Return True when a stop intent has been written to the device's Appium
    node row but the agent has not yet finished tearing the Appium process down.

    The reconciler may write ``desired_state=stopped`` or ``stop_pending=True``
    well before the agent observes the change and stops the Appium process.
    During that window the node row still looks operational (``pid``,
    ``active_connection_target`` populated), so any caller that gates on
    ``operational_state == available`` alone could hand the device to a new run
    only to have the session removed as soon as the process exits. Callers must
    consult this predicate alongside the operational axis.

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


# --- derived-state evaluation (formerly state_derivation.py) ---


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
    return device.verified_at is not None and not in_maintenance(device) and not device.review_required


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

    has_running_session = await device_has_live_session(db, device.id)
    has_verification_lease = await device_has_verification_lease(db, device.id, now=now)

    device_in_maintenance = in_maintenance(device)
    ready = (
        await is_ready_for_use_async(db, device, packs=packs)
        and device_allows_allocation(device)
        and not device.review_required
    )

    return DeviceStateFacts(
        has_running_session=has_running_session,
        has_verification_lease=has_verification_lease,
        in_maintenance=device_in_maintenance,
        stop_in_flight=appium_node_stop_in_flight(device),
        ready=ready,
    )


async def apply_derived_state(
    db: AsyncSession,
    device: Device,
    *,
    now: datetime,
    publisher: EventPublisher,
    packs: dict[str, DriverPack] | None = None,
) -> bool:
    """Derive ``operational_state`` and write it when it differs from the persisted column.

    Emits the operational bus event when the axis changes. Transitions are uncaused: causes
    are recorded once, at the observation sites that know them (connectivity sweep, host
    heartbeat loss, lifecycle escalation, maintenance service). The reconciler records no
    DeviceEvent audit rows — a not-ready offline transition could be a connectivity loss, a
    node crash, a health-probe failure or a verification failure, and guessing here would
    corrupt the analytics reliability counts.

    Returns True if the operational axis was written.
    """
    facts = await gather_device_state_facts(db, device, now=now, packs=packs)
    derived_op = evaluate_operational_state(facts)

    if derived_op is device.operational_state:
        return False

    await set_operational_state(device, derived_op, publisher=publisher)
    return True
