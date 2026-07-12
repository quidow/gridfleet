"""Read-time projection and event-ledger helpers for the device operational-state axis.

operational_state -- what the device is doing (available/busy/verifying/maintenance/offline).

Events are queued through the SQLAlchemy session so they fire on commit, not
before. Bypassing the queue causes ghost transitions when the surrounding
transaction rolls back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from sqlalchemy import and_, case, exists, or_, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import selectinload

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core.observability import get_logger
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.claims import (
    device_has_live_session,
    device_has_verification_lease,
    live_session_exists,
    live_session_predicate,
    verification_lease_exists,
)
from app.devices.services.health_view import device_allows_allocation
from app.devices.services.lifecycle_policy_state import in_maintenance
from app.devices.services.readiness import is_ready_for_use_async, load_packs_by_ids
from app.sessions.models import Session

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session as OrmSession
    from sqlalchemy.sql.expression import ColumnElement

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
class WithdrawalFacts:
    """The operator-withdrawal fact group — everything that pulls a device out
    of service, and nothing about node health (row-local, no IO).

    ``device_in_service`` (the ``baseline:idle`` gate, F-G1) is the projection
    over exactly this group: a withdrawn device must never receive a
    baseline-started node. Node health is deliberately not a member —
    ``device_allows_allocation`` inspects the node, so gating baseline starts
    on it would deadlock a stopped node against ever baseline-starting. The
    full-readiness projection folds this same group into
    ``DeviceStateFacts.ready`` (see ``gather_device_state_facts``), so a fact
    added here reaches both projections structurally.
    """

    verified: bool  # verified_at IS NOT NULL
    in_maintenance: bool  # lifecycle_policy_state["maintenance_reason"] set
    review_required: bool

    @classmethod
    def from_device(cls, device: Device) -> WithdrawalFacts:
        return cls(
            verified=device.verified_at is not None,
            in_maintenance=in_maintenance(device),
            review_required=device.review_required,
        )

    def in_service(self) -> bool:
        return self.verified and not self.in_maintenance and not self.review_required


@dataclass(frozen=True)
class DeviceStateFacts:
    """All inputs the device-state derivation needs, pre-gathered (no IO here)."""

    has_running_session: bool  # a Session row status=running, ended_at IS NULL
    has_verification_lease: bool  # an active verification intent (§16 task 4)
    in_maintenance: bool  # lifecycle_policy_state["maintenance_reason"] set (§16.1)
    stop_in_flight: bool  # appium_node_stop_in_flight(device)
    ready: bool  # is_ready_for_use ∧ device_allows_allocation ∧ WithdrawalFacts.in_service()


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

    withdrawal = WithdrawalFacts.from_device(device)
    # in_service() adds only implied or masked conjuncts over the previous
    # formula (verified is implied by is_ready_for_use; ¬in_maintenance is
    # masked by the evaluator's maintenance rung, and ready has no other
    # consumer) — kept so the withdrawal group is consumed whole and a new
    # withdrawal fact cannot reach one projection without the other.
    ready = (
        await is_ready_for_use_async(db, device, packs=packs)
        and device_allows_allocation(device)
        and withdrawal.in_service()
    )

    return DeviceStateFacts(
        has_running_session=has_running_session,
        has_verification_lease=has_verification_lease,
        in_maintenance=withdrawal.in_maintenance,
        stop_in_flight=appium_node_stop_in_flight(device),
        ready=ready,
    )


# --- SQL twin of the evaluator (WS-7.2). Keep each leg in lockstep with the
# --- corresponding fact in gather_device_state_facts.


def maintenance_sql() -> ColumnElement[bool]:
    """SQL form of ``WithdrawalFacts.in_maintenance``."""
    return cast("ColumnElement[bool]", Device.lifecycle_policy_state["maintenance_reason"].astext.is_not(None))


def stop_in_flight_sql() -> ColumnElement[bool]:
    """SQL form of ``appium_node_stop_in_flight``."""
    return exists(
        select(AppiumNode.id)
        .where(
            AppiumNode.device_id == Device.id,
            or_(AppiumNode.desired_state == AppiumDesiredState.stopped, AppiumNode.stop_pending.is_(True)),
        )
        .correlate(Device)
    )


def allows_allocation_sql() -> ColumnElement[bool]:
    """SQL form of ``device_allows_allocation``."""
    node_present_not_running = exists(
        select(AppiumNode.id)
        .where(
            AppiumNode.device_id == Device.id,
            or_(
                AppiumNode.health_running.is_(False),
                and_(
                    AppiumNode.health_running.is_(None),
                    or_(AppiumNode.pid.is_(None), AppiumNode.active_connection_target.is_(None)),
                ),
            ),
        )
        .correlate(Device)
    )
    return and_(
        Device.device_checks_healthy.is_not(False),
        ~node_present_not_running,
        or_(Device.session_viability_status.is_(None), Device.session_viability_status != "failed"),
    )


def _ready_sql() -> ColumnElement[bool]:
    # SQL approximation of DeviceStateFacts.ready. The pack-manifest setup-fields
    # axis of is_ready_for_use is not SQL-expressible; verified_at stands in.
    return and_(
        Device.verified_at.is_not(None),
        ~maintenance_sql(),
        Device.review_required.is_(False),
        allows_allocation_sql(),
    )


def is_busyish_sql() -> ColumnElement[bool]:
    return live_session_exists()


def is_verifying_sql(*, now: datetime) -> ColumnElement[bool]:
    return and_(~live_session_exists(), verification_lease_exists(now=now))


def is_maintenance_sql(*, now: datetime) -> ColumnElement[bool]:
    return and_(~live_session_exists(), ~verification_lease_exists(now=now), maintenance_sql())


def is_offline_sql(*, now: datetime) -> ColumnElement[bool]:
    return and_(
        ~live_session_exists(),
        ~verification_lease_exists(now=now),
        ~maintenance_sql(),
        or_(stop_in_flight_sql(), ~_ready_sql()),
    )


def is_available_sql(*, now: datetime) -> ColumnElement[bool]:
    return and_(
        ~live_session_exists(),
        ~verification_lease_exists(now=now),
        ~maintenance_sql(),
        ~stop_in_flight_sql(),
        _ready_sql(),
    )


def operational_state_sql(*, now: datetime) -> ColumnElement[str]:
    """5-way CASE mirroring ``evaluate_operational_state``'s masking order."""
    return case(
        (live_session_exists(), DeviceOperationalState.busy.value),
        (verification_lease_exists(now=now), DeviceOperationalState.verifying.value),
        (maintenance_sql(), DeviceOperationalState.maintenance.value),
        (or_(stop_in_flight_sql(), ~_ready_sql()), DeviceOperationalState.offline.value),
        else_=DeviceOperationalState.available.value,
    )


def operational_state_rank_sql(*, now: datetime) -> ColumnElement[int]:
    """ORDER BY key matching the native enum declaration order."""
    return case(
        (live_session_exists(), 1),
        (verification_lease_exists(now=now), 3),
        (maintenance_sql(), 4),
        (or_(stop_in_flight_sql(), ~_ready_sql()), 2),
        else_=0,
    )


async def derive_operational_state(
    db: AsyncSession,
    device: Device,
    *,
    now: datetime,
    packs: dict[str, DriverPack] | None = None,
) -> DeviceOperationalState:
    """Read-time operational state for one loaded device row."""
    return evaluate_operational_state(await gather_device_state_facts(db, device, now=now, packs=packs))


async def derive_operational_states(
    db: AsyncSession,
    devices: Sequence[Device],
    *,
    now: datetime,
    packs: dict[str, DriverPack] | None = None,
) -> dict[uuid.UUID, DeviceOperationalState]:
    """Batch read-time operational state with bulk claim lookups."""
    ids = [device.id for device in devices]
    if not ids:
        return {}

    live_ids = set(
        (await db.execute(select(Session.device_id).where(Session.device_id.in_(ids), live_session_predicate())))
        .scalars()
        .all()
    )
    leased_ids = set(
        (await db.execute(select(Device.id).where(Device.id.in_(ids), verification_lease_exists(now=now))))
        .scalars()
        .all()
    )
    if packs is None:
        packs = await load_packs_by_ids(db, {device.pack_id for device in devices if device.pack_id})
    result: dict[uuid.UUID, DeviceOperationalState] = {}
    for device in devices:
        withdrawal = WithdrawalFacts.from_device(device)
        ready = (
            (await is_ready_for_use_async(db, device, packs=packs))
            and device_allows_allocation(device)
            and withdrawal.in_service()
        )
        facts = DeviceStateFacts(
            has_running_session=device.id in live_ids,
            has_verification_lease=device.id in leased_ids,
            in_maintenance=withdrawal.in_maintenance,
            stop_in_flight=appium_node_stop_in_flight(device),
            ready=ready,
        )
        result[device.id] = evaluate_operational_state(facts)
    return result


async def emit_operational_state_transition(
    db: AsyncSession,
    device: Device,
    *,
    now: datetime,
    publisher: EventPublisher,
    packs: dict[str, DriverPack] | None = None,
) -> bool:
    """Emit one edge and advance the ledger when the projected state changes.

    The caller must hold the device row lock. Fact-site events carry causes;
    this edge detector intentionally compares only the durable projection.
    """
    derived_op = await derive_operational_state(db, device, now=now, packs=packs)
    old = device.operational_state_last_emitted
    if derived_op is old:
        return False
    session = _persistent_session(device)
    device.operational_state_last_emitted = derived_op
    publisher.queue_for_session(
        session,
        "device.operational_state_changed",
        {
            "device_id": str(device.id),
            "device_name": device.name,
            "old_operational_state": old.value,
            "new_operational_state": derived_op.value,
        },
        severity=_transition_severity(old, derived_op),
    )
    return True
