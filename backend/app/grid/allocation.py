"""Device allocation for W3C new-session requests (grid-router spec §3-4).

The service composes existing machinery — capability matching, the device row
lock, the intent reconciler — and owns no writes to protected state columns:
``busy`` is derived from the ``pending`` Session row by the reconciler.
"""

import json
import logging
import uuid
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, cast

from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import func, null, or_, select, update
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession as DbSession
from sqlalchemy.orm import selectinload

from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services.common import build_grid_stereotype_caps
from app.appium_nodes.services.node_viability import (
    node_accepting_new_sessions_predicate,
    node_viable_predicate,
)
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import (
    Device,
    DeviceGroup,
    DeviceGroupMembership,
    DeviceOperationalState,
    GroupType,
)
from app.devices.schemas.device import HardwareTelemetryState
from app.devices.services import attention as device_attention
from app.devices.services.claims import live_session_exists
from app.devices.services.group_membership import (
    DeviceGroupFacts,
    GroupMembershipIndex,
    evaluate_group_memberships,
    load_groups_by_keys,
    load_static_group_keys_by_device_id,
)
from app.devices.services.intent import IntentService
from app.devices.services.readiness import assess_device_with_pack, assess_devices_async
from app.devices.services.state import is_available_sql
from app.grid import appium_direct
from app.grid.constants import RETRY_INTERVAL_SEC
from app.grid.matching import (
    LEGACY_APPIUM_GRIDFLEET_PREFIX,
    LEGACY_RUN_ID_CAP,
    CapabilityMergeError,
    candidate_matches_stereotype,
    is_match_relevant_key,
    merge_candidates,
    requested_group_keys,
)
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.hosts import service_hardware_telemetry as hardware_telemetry
from app.packs.services.capability import (
    StereotypeTemplate,
    load_pack_catalog,
    load_stereotype_template,
    stereotype_templates_from_packs,
)
from app.packs.services.start_shim import build_device_context, resolve_pack_for_device
from app.runs.models import TERMINAL_STATES, TestRun
from app.runs.service_reservation import reservation_gating_owner_sql
from app.sessions import service as session_service
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session, SessionStatus

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.sql.elements import ColumnElement

    from app.core.protocols import SettingsReader
    from app.devices.services.readiness import DeviceReadiness
    from app.events.protocols import EventPublisher
    from app.packs.models import DriverPack

logger = logging.getLogger(__name__)

_RESTART_WINDOW_FALLBACK_SEC = 120

GRID_ALLOCATION_OUTCOME_TOTAL = Counter(
    "gridfleet_grid_allocation_outcome",
    "Allocation attempt outcomes for new-session requests.",
    labelnames=("outcome",),  # allocated | queued | invalid | expired | claim_expired
)
GRID_QUEUE_DEPTH = Gauge(
    "gridfleet_grid_queue_depth",
    "Waiting tickets in grid_session_queue.",
)
GRID_STEREOTYPE_LOOKUP_ERROR_TOTAL = Counter(
    "gridfleet_grid_stereotype_lookup_error",
    "Pack/platform lookups that failed while rendering a device's slot stereotype "
    "(device falls back to an empty pack stereotype and is unmatchable until repaired).",
)
GRID_ELIGIBLE_DEVICES = Gauge(
    "gridfleet_grid_eligible_devices",
    "Devices eligible for allocation at the most recent allocate attempt "
    "(available, node-viable, no live session). Excludes cooldown/busy devices.",
)
GRID_TRY_ALLOCATE_DURATION_SECONDS = Histogram(
    "gridfleet_grid_try_allocate_duration_seconds",
    "Server-side duration of a single try_allocate attempt (excludes long-poll queue wait).",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
GRID_ALLOCATE_QUEUE_WAIT_SECONDS = Histogram(
    "gridfleet_grid_allocate_queue_wait_seconds",
    "Total wall-clock time a /internal/grid/create-session long-poll waited before returning, by outcome. "
    "Separates capacity scarcity (queue wait) from try_allocate service time.",
    labelnames=("outcome",),  # allocated | queued
    # The long poll runs to LONG_POLL_SEC (25s); extend past the 10s default ceiling (#9).
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 25.0, 30.0),
)


class AllocationNotPendingError(Exception):
    """The allocation id does not reference a pending session row."""

    def __init__(self, allocation_id: uuid.UUID) -> None:
        super().__init__(f"allocation {allocation_id} is not pending")
        self.allocation_id = allocation_id


# A waiting ticket whose client half-closed cannot be detected by the router's
# allocate long-poll, so without a liveness signal it FIFO-vetoes every younger
# waiter until ``grid.queue_timeout_sec``. ``try_allocate`` stamps
# ``last_polled_at`` on every poll; a waiting ticket not re-polled within this
# many poll intervals is treated as dead — both ignored by the FIFO veto and
# expired by the reaper. 10 intervals (~10s at the 1s router poll) is comfortably
# longer than a single slow poll but far shorter than the 300s queue timeout.
TICKET_STALE_POLL_INTERVALS = 10


def _ticket_liveness_cutoff(now: datetime) -> datetime:
    """Waiting tickets last polled before this instant are considered dead clients."""
    return now - timedelta(seconds=RETRY_INTERVAL_SEC * TICKET_STALE_POLL_INTERVALS)


def _legal_ticket_transition(current: GridQueueStatus, to: GridQueueStatus) -> bool:
    """``waiting`` is the only live state: it advances to ``cancelled``
    (invalid body / router gave up) or ``expired`` (reaper). Terminal states are
    sinks -- the lost-response resume path reads
    the Session row by ``ticket_id`` and never rewinds a ticket.
    """
    if current == to:
        return True
    return current == GridQueueStatus.waiting and to in (
        GridQueueStatus.cancelled,
        GridQueueStatus.expired,
    )


def transition_ticket(ticket: GridSessionQueueTicket, to: GridQueueStatus, *, reason: str) -> None:
    """Single seam for every ticket status mutation (harness Q3).

    Asserts the source->target transition is legal (catching the "forgot to
    terminalize on this exit seam" class of bug that scattered ad-hoc assignments
    produced) and emits one debug log. Direct ``ticket.status = ...`` assignment is
    forbidden outside this helper.
    """
    if not _legal_ticket_transition(ticket.status, to):
        raise ValueError(f"illegal ticket transition {ticket.status} -> {to} (ticket={ticket.id}, reason={reason})")
    logger.debug("grid_ticket_transition ticket=%s %s->%s reason=%s", ticket.id, ticket.status, to, reason)
    ticket.status = to


IntentFactory = Callable[[DbSession], IntentService]
# A per-attempt cache of pack-rendered stereotype templates keyed by (pack_id,
# platform_id). The template half is device-independent (#11), so a fleet of
# same-pack devices renders one DB lookup per unique pack/platform per attempt.
# A ``None`` value is a cached *negative*: this pack/platform is known to be
# unresolvable (pack deleted, platform dropped from the release) for the rest of
# the attempt. Without it every device on a broken pack re-issued the failing
# lookup — and that lookup costs two reads, since its LookupError path queries
# again just to choose between "no releases" and "platform not in release".
StereotypeTemplateCache = dict[tuple[str, str], StereotypeTemplate | None]


class StereotypeProvider(Protocol):
    async def __call__(
        self,
        db: DbSession,
        device: Device,
        *,
        template_cache: StereotypeTemplateCache | None = None,
        matching_group_keys: Collection[str] = (),
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AllocationResult:
    allocation_id: uuid.UUID
    target: str
    device_id: uuid.UUID


class RunNotActiveError(Exception):
    """A run-bound allocate names a run that is missing or already terminal."""

    def __init__(self, run_id: uuid.UUID, state: str) -> None:
        super().__init__(f"run {run_id} is {state}; sessions can only be created for a live (non-terminal) run")


def _ticket_passes_reservation(ticket_run_id: uuid.UUID | None, reservation_run_id: uuid.UUID | None) -> bool:
    """Strict symmetric admission (run-scoped-endpoint spec §1/§3): a run-bound
    ticket may take only devices reserved for its run; a free ticket may take
    only unreserved devices. No spillover in either direction."""
    return ticket_run_id == reservation_run_id


@dataclass(frozen=True)
class _EligibleRow:
    """One row of the eligible-devices batch: the device plus the per-device facts
    the group-membership evaluator consumes, projected in the same SQL statement
    so the polling read budget stays constant at fleet scale."""

    device: Device
    reservation_run_id: uuid.UUID | None
    static_group_keys: frozenset[str]


def _static_group_keys_subquery(group_keys: Collection[str]) -> ColumnElement[object]:
    """Correlated scalar subquery: the array of static group keys (filtered to
    *group_keys*) this device is a member of, or NULL when none match."""
    return cast(
        "ColumnElement[object]",
        (
            select(func.array_agg(DeviceGroup.key))
            .select_from(DeviceGroupMembership)
            .join(DeviceGroup, DeviceGroup.id == DeviceGroupMembership.group_id)
            .where(
                DeviceGroupMembership.device_id == Device.id,
                DeviceGroup.group_type == GroupType.static,
                DeviceGroup.key.in_(list(group_keys)),
            )
            .correlate(Device)
            .scalar_subquery()
        ),
    )


def _null_array_subquery() -> ColumnElement[object]:
    """A scalar subquery that always returns NULL — used as a placeholder for the
    static-group-keys column when no group keys are requested so the eligible-
    devices query keeps a fixed column shape (no branching row structure)."""
    return cast("ColumnElement[object]", select(null()).scalar_subquery())


def _pack_platform_keys(rows: Collection[_EligibleRow]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for row in rows:
        resolved = resolve_pack_for_device(row.device)
        if resolved is not None:
            keys.add(resolved)
    return keys


def _facts_from_eligible_rows(
    rows: Collection[_EligibleRow],
    readiness_by_device_id: Mapping[uuid.UUID, DeviceReadiness],
    settings: SettingsReader | None,
) -> dict[uuid.UUID, DeviceGroupFacts]:
    """Build the pure evaluator's fact inputs from the eligible batch. No database
    IO: every fact the evaluator consumes is either a loaded device/node/host
    column, a projected reservation owner, a projected static key set, a
    synchronous settings-derived telemetry value, or a pre-assessed readiness
    verdict from the caller's pack catalog.

    ``operational_state`` is ``available`` by construction, not by assumption:
    ``is_available_sql`` is exactly the ``else_`` branch of
    ``operational_state_sql``, so every row that cleared the eligibility gate is
    genuinely available. ``readiness_state`` is *not* similarly implied —
    ``_ready_sql`` uses ``verified_at`` as a stand-in and its own comment notes
    the pack-manifest setup-fields axis is not SQL-expressible — so it must be
    derived per device (``app.devices.services.readiness``, the same logic
    ``load_group_membership_index`` uses) rather than asserted as ``verified``.
    """
    facts: dict[uuid.UUID, DeviceGroupFacts] = {}
    for row in rows:
        device = row.device
        if settings is None:
            hardware_telemetry_state = HardwareTelemetryState.unknown
        else:
            hardware_telemetry_state = hardware_telemetry.hardware_telemetry_state_for_device(device, settings=settings)
        hardware_health_status = hardware_telemetry.current_hardware_health_status(device)
        readiness = readiness_by_device_id.get(device.id)
        readiness_state = readiness.readiness_state if readiness is not None else "setup_required"
        needs_attention = device_attention.compute_needs_attention(
            DeviceOperationalState.available,
            readiness_state,
            hardware_health_status=hardware_health_status,
            review_required=bool(device.review_required),
        )
        facts[device.id] = DeviceGroupFacts(
            operational_state=DeviceOperationalState.available,
            is_reserved=row.reservation_run_id is not None,
            readiness_state=readiness_state,
            hardware_telemetry_state=hardware_telemetry_state,
            needs_attention=needs_attention,
            static_group_keys=row.static_group_keys,
        )
    return facts


def _ready_rows(
    rows: Sequence[_EligibleRow],
    facts_by_device_id: Mapping[uuid.UUID, DeviceGroupFacts],
) -> list[_EligibleRow]:
    """Drop eligible rows whose real readiness verdict is not ``verified``.

    ``is_available_sql`` — the eligibility predicate and the claim-lock predicate
    both — is only an approximation of ``DeviceStateFacts.ready``: ``verified_at``
    stands in for the pack-manifest setup-fields axis, which is not
    SQL-expressible. A device missing a ``required_for_session`` field therefore
    clears SQL on both sides of the lock while the Python evaluator derives
    ``setup_required`` -> ``offline`` and the Devices page shows it offline;
    claiming it creates a session against a device that cannot run one.

    The verdict is the one ``_eligible_facts`` already assessed from the poll's
    pack catalog, so this gate costs no read. Mirrors the run allocator's step-5
    gate in ``app.runs.service_allocator._batch_select_devices``.
    """
    return [
        row for row in rows if (facts := facts_by_device_id.get(row.device.id)) and facts.readiness_state == "verified"
    ]


def _matching_group_keys_for_device(
    membership: GroupMembershipIndex,
    device_id: uuid.UUID,
    group_keys: Collection[str],
) -> list[str]:
    """Project the subset of *group_keys* the device belongs to (membership
    AND over each key). The stereotype advertises only these keys as
    ``gridfleet:group:<key>`` caps."""
    return [key for key in group_keys if membership.matches_all(device_id, [key])]


class AllocationService:
    def __init__(
        self,
        *,
        intent_factory: IntentFactory,
        publisher: EventPublisher,
        stereotype_provider: StereotypeProvider,
        settings: SettingsReader | None = None,
    ) -> None:
        self._intent_factory = intent_factory
        self._publisher = publisher
        self._stereotype_provider = stereotype_provider
        self._settings = settings

    def _restart_window_sec(self) -> int:
        if self._settings is None:
            return _RESTART_WINDOW_FALLBACK_SEC
        try:
            return int(cast("int", self._settings.get("appium_reconciler.restart_window_sec")))
        except KeyError:
            return _RESTART_WINDOW_FALLBACK_SEC

    async def promote_to_running(
        self,
        db: DbSession,
        *,
        allocation_id: uuid.UUID,
        appium_session_id: str,
        appium_capabilities: dict[str, Any] | None = None,
    ) -> None:
        """Swap the placeholder session id for the Appium id and promote to ``running``.

        The status transition is a conditional UPDATE guarded on ``status='pending'``
        so the reaper failing the row mid-create loses the race deterministically.
        A retry of an interrupted create is handled by ``resume_interrupted`` before
        a fresh claim; a non-pending row therefore raises ``AllocationNotPendingError``.

        ``last_activity_at`` is intentionally NOT stamped at promotion: a ``running``
        row with NULL activity means "the client never issued a command". The
        router's server-stamped ``/internal/grid/activity`` flush is the only
        writer, and ``SessionSyncService._check_liveness`` reaps a never-commanded
        session after ``grid.session_first_command_grace_sec`` (measured from the
        claim-time ``started_at``).
        """
        if appium_capabilities is not None:
            size = len(json.dumps(appium_capabilities, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            if size > 32 * 1024:
                appium_capabilities = None
        try:
            result = await db.execute(
                update(Session)
                .where(Session.id == allocation_id, Session.status == SessionStatus.pending)
                .values(
                    session_id=appium_session_id,
                    status=SessionStatus.running,
                    actual_capabilities=appium_capabilities,
                )
            )
            await db.flush()
        except IntegrityError:
            # Roll back the poisoned transaction and surface the allocation conflict.
            await db.rollback()
            raise AllocationNotPendingError(allocation_id) from None
        if int(getattr(result, "rowcount", 0) or 0) == 0:
            raise AllocationNotPendingError(allocation_id)
        # This is the authoritative creation point for router-issued sessions (spec
        # §8): emit session.started here so consumers fire for clients that never hit
        # the legacy register API (Appium Inspector, plain WebDriver). Reload with the
        # device eagerly so the event payload renders without a lazy IO.
        session = (
            (await db.execute(select(Session).options(selectinload(Session.device)).where(Session.id == allocation_id)))
            .scalars()
            .one()
        )
        session_service.queue_session_started_event(
            db,
            session,
            device=session.device,
            run_id=str(session.run_id) if session.run_id is not None else None,
            publisher=self._publisher,
        )

    async def fail(self, db: DbSession, *, allocation_id: uuid.UUID, message: str) -> None:
        # Lock first (as before), then attempt the conditional transition. The device
        # lock + reconcile only fire on a successful transition: rowcount 0 means the
        # row was already promoted/reaped, so we no-op (idempotent) and skip reconcile.
        row = await db.get(Session, allocation_id)
        if row is None:
            return
        device_id = row.device_id
        if device_id is not None:
            await device_locking.lock_device(db, device_id)
        result = await db.execute(
            update(Session)
            .where(Session.id == allocation_id, Session.status == SessionStatus.pending)
            .values(
                status=SessionStatus.error,
                error_type="allocation_failed",
                error_message=message,
                ended_at=now_utc(),
            )
        )
        if int(getattr(result, "rowcount", 0) or 0) == 0:
            return  # idempotent: already promoted/reaped
        await db.refresh(row)
        await db.flush()
        if device_id is not None:
            intent = self._intent_factory(db)
            await intent.reconcile_now(device_id, publisher=self._publisher)

    async def mark_ended(self, db: DbSession, *, appium_session_id: str) -> None:
        """Close a running session the same way session_sync closes vanished sessions.

        The router's ended notification carries no outcome (a W3C DELETE has none),
        so the shared close path defaults to ``passed`` — unless the owning run
        already reached a non-completed terminal state, in which case the session
        was aborted out from under the client and is closed ``error`` (#7).
        """
        stmt = (
            select(Session)
            .options(selectinload(Session.device), selectinload(Session.run))
            .where(
                Session.session_id == appium_session_id,
                Session.status == SessionStatus.running,
                Session.ended_at.is_(None),
            )
        )
        row = (await db.execute(stmt)).scalars().first()
        if row is None:
            return
        await session_service.close_running_session(db, row, attached_run=row.run, publisher=self._publisher)

    async def reap_expired(self, db: DbSession) -> dict[str, int]:
        # Fails expired claims one by one (each `fail` reconciles + flushes). Batch
        # size is naturally bounded by the reaper's 5s interval; don't batch unless
        # that interval grows.
        if self._settings is None:
            raise RuntimeError("AllocationService.reap_expired requires a settings reader")
        claim_window = int(cast("int", self._settings.get("grid.claim_window_sec")))
        queue_timeout = int(cast("int", self._settings.get("grid.queue_timeout_sec")))
        now = now_utc()

        pending_stmt = select(Session.id).where(
            Session.status == SessionStatus.pending,
            Session.ended_at.is_(None),
            # Crash orphans only: live create is bounded below claim_window by
            # effective_create_timeout() in session_create.py.
            Session.started_at < now - timedelta(seconds=claim_window),
        )
        pending_failed = 0
        for (session_pk,) in (await db.execute(pending_stmt)).all():
            await self.fail(db, allocation_id=session_pk, message="allocation claim window expired")
            GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="claim_expired").inc()
            pending_failed += 1

        # Expire waiting tickets that are either past the queue timeout (the client
        # waited too long) OR not re-polled within a few poll intervals (a dead /
        # half-closed client the router's long-poll cannot detect). The second
        # condition keeps an abandoned ticket from FIFO-vetoing live younger waiters
        # for the full queue timeout (harness C8). A ticket never polled yet
        # (last_polled_at IS NULL) is only a few created_at-old at most — covered by
        # the queue-timeout arm, so NULL is not treated as stale.
        stale_cutoff = _ticket_liveness_cutoff(now)
        tickets_stmt = select(GridSessionQueueTicket).where(
            GridSessionQueueTicket.status == GridQueueStatus.waiting,
            or_(
                GridSessionQueueTicket.created_at < now - timedelta(seconds=queue_timeout),
                GridSessionQueueTicket.last_polled_at < stale_cutoff,
            ),
        )
        tickets_expired = 0
        for stale in (await db.execute(tickets_stmt)).scalars():
            transition_ticket(stale, GridQueueStatus.expired, reason="reaper_queue_timeout_or_stale_poll")
            GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="expired").inc()
            tickets_expired += 1
        await db.flush()
        return {
            "pending_failed": pending_failed,
            "tickets_expired": tickets_expired,
        }

    def _validate_candidates(
        self,
        ticket: GridSessionQueueTicket,
        candidates: list[dict[str, Any]],
    ) -> frozenset[str]:
        """Tombstone every clean-break rejection (legacy caps, invalid group
        selectors) and return the current ticket's direct group keys. Raises
        ``CapabilityMergeError`` for any rejection so the API layer surfaces a
        descriptive 400 body. Stamps the liveness heartbeat before any early
        return so even an invalid-body ticket records that its client was
        present this tick.
        """
        # Clean-break tombstone (spec §1): reject cap-era clients loudly.
        if any(LEGACY_RUN_ID_CAP in c for c in candidates):
            transition_ticket(ticket, GridQueueStatus.cancelled, reason="legacy_run_id_cap")
            GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="invalid").inc()
            raise CapabilityMergeError(
                "the gridfleet:run_id capability is no longer supported; "
                "create run sessions through the router's /run/{run_id} endpoint"
            )
        # Clean-break tombstone: the retired ``appium:gridfleet:`` cap namespace
        # moved to the bare ``gridfleet:`` prefix. Reject the old prefix loudly so
        # a stale pin fails fast instead of silently matching any device.
        if any(k.startswith(LEGACY_APPIUM_GRIDFLEET_PREFIX) for c in candidates for k in c):
            transition_ticket(ticket, GridQueueStatus.cancelled, reason="legacy_appium_gridfleet_cap")
            GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="invalid").inc()
            raise CapabilityMergeError(
                "the appium:gridfleet:* capability namespace is no longer supported; "
                "use the gridfleet:* prefix instead (e.g. gridfleet:deviceId)"
            )
        # Validate the routable device-group selectors (gridfleet:group:<key>) and
        # tombstone the retired gridfleet:tag:* capability. Malformed syntax or a
        # non-boolean-true value cancels this ticket with a descriptive 400 message.
        try:
            return requested_group_keys(candidates)
        except CapabilityMergeError as e:
            logger.warning("grid_allocation_invalid_group_selector ticket=%s detail=%s", ticket.id, e)
            transition_ticket(ticket, GridQueueStatus.cancelled, reason="invalid_capabilities")
            GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="invalid").inc()
            raise

    async def try_allocate(
        self,
        db: DbSession,
        *,
        ticket: GridSessionQueueTicket,
        exclude_device_ids: set[uuid.UUID] | None = None,
    ) -> AllocationResult | None:
        # Liveness heartbeat: a still-polling client proves it is alive on every tick.
        # The FIFO veto and the reaper read last_polled_at to expire abandoned tickets
        # long before queue_timeout (harness C8). Stamp before any early return so even
        # an invalid-body ticket records that its client was present this tick.
        ticket.last_polled_at = now_utc()
        # A run-bound ticket is only as alive as its run: re-check every tick so a
        # run cancelled/completed mid-queue fails its waiters NOW with a clear
        # message instead of stranding them until the queue timeout (spec §4).
        # A still-`preparing` run is a legitimate session source — clients open
        # Appium sessions on their reserved devices during preparation (install
        # builds, sign in, smoke checks; docs: runs-and-reservations.md §preparing).
        # Only a missing or already-terminal run is a hard reject. This same check
        # is the creation-time validation — the allocate endpoint calls try_allocate
        # in the request that creates the ticket.
        #
        # One scalar ``SELECT Run.state`` (no relationship loader) — a poll is hot
        # and the per-attempt read budget is the central deliverable of this task.
        if ticket.run_id is not None:
            run_state = (
                await db.execute(select(TestRun.state).where(TestRun.id == ticket.run_id))
            ).scalar_one_or_none()
            if run_state is None or run_state in TERMINAL_STATES:
                state = run_state.value if run_state is not None else "missing"
                transition_ticket(ticket, GridQueueStatus.cancelled, reason="run_not_active")
                GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="invalid").inc()
                raise RunNotActiveError(ticket.run_id, state)
        try:
            candidates = merge_candidates(ticket.requested_body)
        except CapabilityMergeError as e:
            logger.warning("grid_allocation_invalid_body ticket=%s detail=%s", ticket.id, e)
            transition_ticket(ticket, GridQueueStatus.cancelled, reason="invalid_capabilities")
            GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="invalid").inc()
            # Re-raise so the API layer can put the descriptive merge message
            # (e.g. "'firstMatch' must be a list of objects") in the 400 body
            # instead of a generic text (wave-5 #26). The ticket is already
            # cancelled; the caller commits before responding.
            raise
        current_group_keys = self._validate_candidates(ticket, candidates)
        # Hoist the older-waiter load + per-ticket candidate merge out of the
        # per-device x per-candidate loops: load once, pre-merge once, reuse.
        older_candidate_sets = await self._older_waiter_candidate_sets(db, ticket)
        older_group_keys: set[str] = set()
        for _, older_candidates in older_candidate_sets:
            older_group_keys |= requested_group_keys(older_candidates)
        group_keys = set(current_group_keys) | older_group_keys
        # The group-definition load returns direct requested groups plus the static
        # groups named by their JSON ``member_of`` arrays in one CTE/query so the
        # evaluator can resolve dynamic groups that reference static groups by key.
        groups = await self._load_groups_by_key(db, group_keys) if group_keys else []
        loaded_group_keys = {group.key for group in groups}
        # Reject the current ticket loudly when any of its direct group keys does
        # not exist (the missing-key/unknown-key rejection Task 3 deferred). A
        # missing key is a client error (HTTP 400), not a silent no-match.
        missing_current = set(current_group_keys) - loaded_group_keys
        if missing_current:
            logger.warning(
                "grid_allocation_unknown_group_key ticket=%s keys=%s",
                ticket.id,
                sorted(missing_current),
            )
            transition_ticket(ticket, GridQueueStatus.cancelled, reason="unknown_group_key")
            GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="invalid").inc()
            raise CapabilityMergeError(f"unknown device group key: {sorted(missing_current)[0]!r}")
        # Drop older waiter candidate sets whose direct group keys are missing
        # without invalidating the current ticket — a stale older waiter cannot
        # block younger valid work; it cancels itself on its own next poll.
        older_candidate_sets = [
            (run_id, older_candidates)
            for run_id, older_candidates in older_candidate_sets
            if requested_group_keys(older_candidates) <= loaded_group_keys
        ]
        # One eligible-devices query projecting reservation owner and the static
        # group-key aggregation for the direct/member_of keys, in the same SQL
        # statement (read 3). The pack-template batch (read 4) folds the per-
        # device pack/platform lookups into one SELECT so the read count is
        # constant at fleet scale.
        eligible_rows = await self._eligible_devices_with_facts(
            db,
            group_keys=loaded_group_keys,
            exclude_device_ids=exclude_device_ids,
        )
        templates, facts_by_device_id, pack_catalog = await self._eligible_facts(db, eligible_rows)
        eligible_rows = _ready_rows(eligible_rows, facts_by_device_id)
        membership = evaluate_group_memberships(
            groups=groups,
            devices=[row.device for row in eligible_rows],
            facts_by_device_id=facts_by_device_id,
        )
        # Pre-populate the template cache so device_match_surface finds every
        # needed template without issuing an extra per-key read.
        template_cache: StereotypeTemplateCache = dict(templates)
        stereotype_cache: dict[uuid.UUID, dict[str, Any]] = {}
        for row in eligible_rows:
            device = row.device
            stereotype = stereotype_cache.get(device.id)
            if stereotype is None:
                matching_keys = _matching_group_keys_for_device(membership, device.id, group_keys)
                stereotype = await self._stereotype_provider(
                    db,
                    device,
                    template_cache=template_cache,
                    matching_group_keys=matching_keys,
                )
                stereotype_cache[device.id] = stereotype
            reservation_run_id = row.reservation_run_id
            for candidate in candidates:
                if not (
                    candidate_matches_stereotype(candidate, stereotype)
                    and _ticket_passes_reservation(ticket.run_id, reservation_run_id)
                ):
                    continue
                # FIFO veto, reservation-aware: only count older waiters that could
                # actually take THIS device — i.e. whose ticket clears the same
                # reservation gate and whose candidate matches the stereotype.
                if self._older_waiter_blocks(older_candidate_sets, stereotype, reservation_run_id):
                    continue
                result = await self._claim(
                    db,
                    ticket=ticket,
                    row=row,
                    candidate=candidate,
                    run_id=ticket.run_id,
                    exclude_device_ids=exclude_device_ids,
                    groups=groups,
                    pack_catalog=pack_catalog,
                )
                if result is not None:
                    GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="allocated").inc()
                    return result
        return None

    async def resume_interrupted(self, db: DbSession, *, ticket_id: uuid.UUID) -> None:
        """Resolve a live row from an interrupted create before a fresh claim."""
        stmt = (
            select(Session)
            .options(selectinload(Session.device).selectinload(Device.appium_node))
            .options(selectinload(Session.device).selectinload(Device.host))
            .where(
                Session.ticket_id == ticket_id,
                live_session_predicate(),
            )
        )
        row = (await db.execute(stmt)).scalars().first()
        if row is None:
            return
        if row.status == SessionStatus.pending:
            await self.fail(db, allocation_id=row.id, message="create interrupted; retried by router")
            return
        target = resolve_router_target(row)
        if target is not None:
            await appium_direct.terminate_session(target, row.session_id)
        await self.mark_ended(db, appium_session_id=row.session_id)

    async def _load_groups_by_key(self, db: DbSession, group_keys: Collection[str]) -> list[DeviceGroup]:
        """One read: the requested groups plus the static groups their JSON
        ``member_of`` arrays reference, so the pure evaluator can resolve dynamic
        groups that reference static groups by key. Direct keys of any type are
        returned verbatim; only static groups are pulled from ``member_of``
        (dynamic-to-dynamic references resolve to empty membership by contract).

        Delegates to the shared recursive-CTE helper in
        :mod:`app.devices.services.group_membership` so the closure logic lives
        in one place.
        """
        return await load_groups_by_keys(db, group_keys)

    async def _eligible_devices_with_facts(
        self,
        db: DbSession,
        *,
        group_keys: Collection[str],
        exclude_device_ids: set[uuid.UUID] | None = None,
    ) -> list[_EligibleRow]:
        """One read: every eligible ``Device`` (joined to its AppiumNode + Host)
        plus the reservation-gating owner run id and the per-device static group
        keys for the direct/member_of keys, projected in the same SQL statement.
        The pure membership evaluator consumes these facts without issuing any
        further reads.
        """
        now = now_utc()
        reservation_subq = reservation_gating_owner_sql(now=now)
        static_keys_subq = _static_group_keys_subquery(group_keys) if group_keys else _null_array_subquery()
        stmt = (
            select(
                Device,
                static_keys_subq.label("static_group_keys"),
                reservation_subq.label("reservation_run_id"),
            )
            .outerjoin(AppiumNode, AppiumNode.device_id == Device.id)
            .where(is_available_sql(now=now))
            .where(node_viable_predicate(now=now, restart_window_sec=self._restart_window_sec()))
            .where(node_accepting_new_sessions_predicate())
            .where(~live_session_exists())
        )
        if exclude_device_ids:
            stmt = stmt.where(~Device.id.in_(exclude_device_ids))
        rows: list[_EligibleRow] = []
        for device, static_keys, reservation_run_id in (await db.execute(stmt)).all():
            key_set = frozenset() if static_keys is None else frozenset(str(k) for k in static_keys)
            rows.append(
                _EligibleRow(
                    device=device,
                    reservation_run_id=reservation_run_id,
                    static_group_keys=key_set,
                )
            )
        GRID_ELIGIBLE_DEVICES.set(len(rows))
        return rows

    async def _eligible_facts(
        self,
        db: DbSession,
        rows: Sequence[_EligibleRow],
    ) -> tuple[dict[tuple[str, str], StereotypeTemplate], dict[uuid.UUID, DeviceGroupFacts], dict[str, DriverPack]]:
        """One read: the pack catalog for every eligible row's pack, projected into
        both the stereotype templates the matcher needs and the readiness verdicts
        the group evaluator needs.

        This is the poll's fourth and last read: one ``load_pack_catalog`` call,
        projected twice. ``stereotype_templates_from_packs`` renders the matcher's
        templates from it, and ``assess_devices_async`` derives readiness from the
        same rows (it issues no read when the caller supplies ``packs``), so the
        free group-routed poll stays at four reads. Loading templates alone would
        force readiness to be re-read or — as it once was — hardcoded.

        The catalog is returned so the claim path can re-assess a locked row
        without another read: ``_claim`` re-runs ``assess_device_with_pack``
        against it under the device row lock, and ``_locked_membership_holds``
        reuses it for the same reason.
        """
        pack_platform_keys = _pack_platform_keys(rows)
        # Key the catalog off every row's pack_id, exactly as
        # ``load_group_membership_index`` does. Deriving it from
        # ``pack_platform_keys`` instead would drop any device whose
        # (pack_id, platform_id) pair fails to resolve, and readiness would then
        # assess that device against a missing pack — a divergence between the
        # allocator's needs_attention verdict and the Devices page's.
        pack_catalog = await load_pack_catalog(db, {row.device.pack_id for row in rows if row.device.pack_id})
        templates = stereotype_templates_from_packs(pack_catalog, pack_platform_keys)
        readiness = await assess_devices_async(db, [row.device for row in rows], packs=pack_catalog)
        return templates, _facts_from_eligible_rows(rows, readiness, self._settings), pack_catalog

    async def _older_waiter_candidate_sets(
        self, db: DbSession, ticket: GridSessionQueueTicket
    ) -> list[tuple[uuid.UUID | None, list[dict[str, Any]]]]:
        """Pre-merge the firstMatch candidates of every older waiting ticket once.

        Computed once per ``try_allocate`` call (not per device x candidate) and
        reused across the device loop. O(older waiting tickets x firstMatch count)
        is deliberately unbounded — queue depth is bounded by
        grid.queue_timeout_sec reaping; revisit only if metrics show it dominating.
        Tickets with an invalid body are dropped (they cannot block anyone).

        Stale-polled tickets are excluded (harness C8): a ticket whose client
        half-closed cannot be detected by the router long-poll, and the reaper has
        not yet expired it, so without this filter one dead client would FIFO-veto
        every younger live waiter for up to grid.queue_timeout_sec. A ticket not
        re-polled within the liveness window is presumed dead and cannot block. A
        not-yet-polled ticket (last_polled_at IS NULL) only blocks while its
        created_at is itself within the window — i.e. it really is a fresh waiter.
        """
        cutoff = _ticket_liveness_cutoff(now_utc())
        stmt = (
            select(GridSessionQueueTicket)
            .where(
                GridSessionQueueTicket.status == GridQueueStatus.waiting,
                GridSessionQueueTicket.created_at < ticket.created_at,
                or_(
                    GridSessionQueueTicket.last_polled_at >= cutoff,
                    (GridSessionQueueTicket.last_polled_at.is_(None)) & (GridSessionQueueTicket.created_at >= cutoff),
                ),
            )
            .order_by(GridSessionQueueTicket.created_at)
        )
        sets: list[tuple[uuid.UUID | None, list[dict[str, Any]]]] = []
        for older in (await db.execute(stmt)).scalars():
            try:
                older_candidates = merge_candidates(older.requested_body)
            except CapabilityMergeError:
                continue
            try:
                requested_group_keys(older_candidates)
            except CapabilityMergeError:
                # A stale older ticket (tombstoned tag cap, malformed group key,
                # non-boolean-true group value) cannot block younger valid work.
                # It cancels itself with HTTP 400 on its own next poll.
                continue
            sets.append((older.run_id, older_candidates))
        return sets

    @staticmethod
    def _older_waiter_blocks(
        older_candidate_sets: list[tuple[uuid.UUID | None, list[dict[str, Any]]]],
        stereotype: dict[str, Any],
        reservation_run_id: uuid.UUID | None,
    ) -> bool:
        """FIFO veto: does any older waiter have a candidate that could take this
        device? Reservation-aware — an older waiter counts only if its ticket
        clears the device's reservation gate AND a candidate matches the
        stereotype, so a free older waiter never blocks a reserved device and a
        run-bound older waiter never blocks an unreserved one."""
        for older_run_id, older_candidates in older_candidate_sets:
            if not _ticket_passes_reservation(older_run_id, reservation_run_id):
                continue
            for c in older_candidates:
                if candidate_matches_stereotype(c, stereotype):
                    return True
        return False

    def _claim_lock_predicates(
        self,
        *,
        reservation_run_id: uuid.UUID | None,
        exclude_device_ids: set[uuid.UUID] | None,
        now: datetime,
    ) -> list[Any]:
        """SQL predicates appended to the joined ``SELECT ... FOR UPDATE OF devices``
        so the SQL-expressible half of the lock-time recheck is folded into the
        lock query. A no-row result means the candidate lost the race or failed
        one of those rechecks (state, node viability/acceptance, exclusion,
        reservation owner).

        ``is_available_sql`` here is the same approximation the eligible query
        uses — ``verified_at`` stands in for the pack-manifest setup-fields axis
        of ``is_ready_for_use``, which is not SQL-expressible. ``_claim`` closes
        that gap after the lock by re-running ``assess_device_with_pack`` on the
        locked row against the poll's already-loaded pack catalog, so no device
        is claimed on the strength of the approximation alone.

        The live-session absence check is intentionally NOT folded here:
        ``FOR UPDATE OF devices`` waits on a device-row lock, but the waiting
        transaction's WHERE clause is evaluated against its statement snapshot.
        A concurrent claim that inserts a session (without updating the device
        row) and commits does not trigger a WHERE re-evaluation, so the waiting
        transaction would not see the new session and double-claim. A fresh
        ``SELECT`` after the lock acquires a new snapshot and observes the
        committed session — that is the one extra read the claim path issues
        before the session INSERT (see ``test_concurrent_allocation_single_winner``).
        """
        predicates: list[Any] = [
            is_available_sql(now=now),
            node_viable_predicate(now=now, restart_window_sec=self._restart_window_sec()),
            node_accepting_new_sessions_predicate(),
        ]
        if exclude_device_ids:
            predicates.append(~Device.id.in_(exclude_device_ids))
        # Reservation-owner predicate: the device's gating reservation run id
        # must match the ticket's run id (NULL = NULL for a free ticket on a
        # free device), so a run-bound ticket cannot land on an unreserved
        # device and vice versa.
        reservation_subq = reservation_gating_owner_sql(now=now)
        if reservation_run_id is None:
            predicates.append(reservation_subq.is_(None))
        else:
            predicates.append(reservation_subq == reservation_run_id)
        return predicates

    async def _locked_membership_holds(
        self,
        db: DbSession,
        *,
        locked_device: Device,
        groups: Sequence[DeviceGroup],
        candidate_group_keys: Collection[str],
        reservation_run_id: uuid.UUID | None,
        pack_catalog: dict[str, DriverPack],
    ) -> bool:
        """Re-evaluate the candidate's requested group keys against the locked row.

        Membership was decided against the pre-lock eligible batch, and the
        ``Device`` row lock does not serialize ``DeviceGroupMembership`` edits,
        so a membership DELETE can commit between that read and the ``Session``
        INSERT. Mirrors the run allocator's post-lock rebuild
        (``app.runs.service_allocator._batch_select_devices`` step 7b): one
        scalar static-group-keys read, on the claim path only — the free-poll
        read budget is untouched. Dynamic-filter axes re-evaluate for free
        against the freshly locked device row, readiness included (the caller's
        pack catalog is reused, so re-assessing the locked row costs no read).
        """
        if not candidate_group_keys:
            return True
        static_keys = await load_static_group_keys_by_device_id(db, [locked_device.id])
        row = _EligibleRow(
            device=locked_device,
            reservation_run_id=reservation_run_id,
            static_group_keys=static_keys.get(locked_device.id, frozenset()),
        )
        readiness = {locked_device.id: assess_device_with_pack(locked_device, pack_catalog.get(locked_device.pack_id))}
        membership = evaluate_group_memberships(
            groups=groups,
            devices=[locked_device],
            facts_by_device_id=_facts_from_eligible_rows([row], readiness, self._settings),
        )
        return membership.matches_all(locked_device.id, candidate_group_keys)

    async def _claim(
        self,
        db: DbSession,
        *,
        ticket: GridSessionQueueTicket,
        row: _EligibleRow,
        candidate: dict[str, Any],
        run_id: uuid.UUID | None,
        exclude_device_ids: set[uuid.UUID] | None = None,
        groups: Sequence[DeviceGroup] = (),
        pack_catalog: dict[str, DriverPack],
    ) -> AllocationResult | None:
        # Fold every SQL-expressible lock-time recheck into the lock query:
        # availability, node viability/acceptance, exclusion, and the
        # reservation-owner gate. A no-row result means the candidate lost the
        # race or failed one of those. The two axes SQL cannot express —
        # pack-manifest readiness and group membership — are rechecked below
        # against the locked row.
        device = row.device
        reservation_run_id = row.reservation_run_id
        now = now_utc()
        predicates = self._claim_lock_predicates(
            reservation_run_id=reservation_run_id,
            exclude_device_ids=exclude_device_ids,
            now=now,
        )
        try:
            locked = await device_locking.lock_device_handle(db, device.id, predicates=predicates)
        except NoResultFound:
            return None
        # Readiness recheck under the lock. ``is_available_sql`` in the lock
        # predicates is the same SQL approximation the eligible query uses, so a
        # device that lost a required pack-manifest setup field between the poll
        # and the claim still clears it. Re-assess the freshly locked row against
        # the catalog ``_eligible_facts`` already loaded — pure, no extra read,
        # mirroring the run allocator's step-7b gate in ``_batch_select_devices``.
        locked_readiness = assess_device_with_pack(locked.device, pack_catalog.get(locked.device.pack_id))
        if locked_readiness.readiness_state != "verified":
            return None
        # Group membership is the one gate the lock query cannot fold in: it
        # lives in a separate table the device-row lock does not serialize.
        if not await self._locked_membership_holds(
            db,
            locked_device=locked.device,
            groups=groups,
            candidate_group_keys=requested_group_keys([candidate]),
            reservation_run_id=reservation_run_id,
            pack_catalog=pack_catalog,
        ):
            return None
        # Fresh-snapshot live-session recheck under the lock: a concurrent claim
        # that inserted its session and committed while this transaction waited
        # for the device row lock is now visible (READ COMMITTED), so this claim
        # declines. One extra read before the INSERT — the concurrency safety
        # the lock-query WHERE cannot provide (see ``_claim_lock_predicates``).
        live_session = (await db.execute(select(Session.id).where(live_session_predicate(locked.device.id)))).first()
        if live_session is not None:
            return None
        target = node_target(locked.device)
        if target is None:
            # An `available` device with no node/host association is broken host/agent
            # state: the ticket keeps waiting while the device looks claimable.
            logger.warning(
                "grid_allocation_no_node_target device=%s ticket=%s (appium_node=%s host=%s)",
                locked.device.id,
                ticket.id,
                locked.device.appium_node is not None,
                locked.device.host is not None,
            )
            return None
        # Surface the client's test label in the Session.test_name column (the Sessions UI
        # reads it). The legacy register_session API took it as an explicit field; the
        # router/grid flow that replaced it must lift it from the requested caps here.
        requested_test_name = candidate.get("gridfleet:testName")
        session_row = Session(
            id=uuid.uuid4(),
            session_id=f"alloc-{uuid.uuid4()}",  # transient in-create marker; unique, never 'running'
            device_id=locked.device.id,
            status=SessionStatus.pending,
            requested_capabilities=candidate,
            test_name=requested_test_name if isinstance(requested_test_name, str) else None,
            run_id=run_id,
            ticket_id=ticket.id,
            # Persist the allocation target so /routes can fall back to it if the
            # device's node port is transiently stale-cleared later (#6).
            router_target=target,
        )
        db.add(session_row)
        await db.flush()
        # The ticket's job ends here: the Session row is the allocation ledger
        # (ticket_id is the router's resume key). Deleting beats a terminal
        # status -- nothing ever reads a finished ticket again.
        await db.delete(ticket)
        await db.flush()
        # Reuse the device lock held by ``locked`` so the inline ledger/desired-
        # state convergence runs under the same row lock (no extra lock read).
        intent = self._intent_factory(db)
        await intent.reconcile_locked(locked, publisher=self._publisher)
        return AllocationResult(allocation_id=session_row.id, target=target, device_id=locked.device.id)


def _match_relevant_base(template: StereotypeTemplate, device: Device) -> dict[str, Any]:
    """Identity/group/platform-routing keys a pack's stereotype base declares — the only
    base keys the allocation matcher (``candidate_matches_stereotype``) consults. Every
    curated pack declares ``appium:platform``, so this renders and interpolates
    per-device on the common path too, not just for uploaded packs. When present, the
    keys are interpolated per-device (reusing the node-start template engine) and
    projected down to just the matcher-relevant subset."""
    keys = [k for k in template.stereotype_base if is_match_relevant_key(k)]
    if not keys:
        return {}
    rendered = template.interpolate(build_device_context(device))
    return {k: rendered[k] for k in keys if k in rendered}


async def device_match_surface(
    db: DbSession,
    device: Device,
    *,
    template_cache: StereotypeTemplateCache | None = None,
    matching_group_keys: Collection[str] = (),
) -> dict[str, Any]:
    """The minimal routing surface the allocator matches a W3C request against.

    Only the keys ``candidate_matches_stereotype`` consults: ``platformName`` (the
    pack's advertised platform-name scalar), ``appium:platform`` (the pack's per-device
    platform_id routing key) plus any other identity keys a pack declares in its
    stereotype base, the manager-owned deviceId, and the device-group caps for the
    keys the membership index says match this device. The rest of the pack stereotype
    (``appium:os_version``/``device_type``/``appium:automationName``) is rendered only at
    node-start (``render_stereotype`` in ``reconciler_agent``), never for matching.

    When the device's pack/platform cannot be resolved (pack deleted, platform dropped
    from the release) the pack half falls back to empty so one broken pack cannot wedge
    allocation for every other device — but the failure is logged and counted
    (``gridfleet_grid_stereotype_lookup_error``) because such a device advertises no
    ``platformName`` and is silently unmatchable until repaired (#1).

    *template_cache*, when supplied, memoizes the device-independent template by
    ``(pack_id, platform_id)`` so a fleet of same-pack devices issues one DB lookup per
    unique pack/platform instead of one per device (#11).

    *matching_group_keys* is the set of device-group keys the membership index says
    match this device; only those keys are advertised as ``gridfleet:group:<key>``
    caps (boolean true). Task 4 wires the membership index; until then the default
    empty collection means no group caps are advertised.
    """
    surface: dict[str, Any] = {}
    resolved = resolve_pack_for_device(device)
    if resolved is not None:
        pack_id, platform_id = resolved
        failure: LookupError | None = None
        if template_cache is not None and resolved in template_cache:
            template = template_cache[resolved]
            if template is None:
                failure = LookupError(f"{pack_id}/{platform_id} unresolvable (cached this attempt)")
        else:
            try:
                template = await load_stereotype_template(db, pack_id=pack_id, platform_id=platform_id)
            except LookupError as exc:
                template, failure = None, exc
            if template_cache is not None:
                # Cache the negative too, so a fleet on a deleted pack costs one
                # failing lookup per pack/platform per attempt, not one per device.
                template_cache[resolved] = template
        if failure is not None:
            # Counted and logged per affected device (not per unique pair) so the
            # metric keeps meaning "devices rendered unmatchable right now".
            GRID_STEREOTYPE_LOOKUP_ERROR_TOTAL.inc()
            logger.warning(
                "grid_stereotype_lookup_error device=%s pack=%s platform=%s: %s",
                device.id,
                pack_id,
                platform_id,
                failure,
            )
        elif template is not None:
            surface["platformName"] = template.platform_name
            surface.update(_match_relevant_base(template, device))
    surface.update(build_grid_stereotype_caps(device, pack_stereotype=None, matching_group_keys=matching_group_keys))
    return surface


def resolve_router_target(row: Session) -> str | None:
    """Routing target for a Session row: prefer the live node target, fall back to the
    target stored at allocation when the device's node port was transiently stale-cleared
    during recovery backoff (#6). A future routing policy (staleness guard, recovery
    preference) lands here once for every consumer.
    """
    live = node_target(row.device) if row.device is not None else None
    return live or row.router_target


def node_target(device: Device) -> str | None:
    """Direct Appium base URL: host address + the Appium process port.

    ``AppiumNode.port`` is the direct Appium server port reported by the agent
    (the agent's ``running_nodes[*].port``).

    ``lock_device`` eager-loads ``appium_node`` and ``host``. Host address uses
    ``host.ip`` — the same expression node registration uses (reconciler_agent).
    """
    node = device.appium_node
    if node is None or node.port is None or device.host is None:
        return None
    return f"http://{device.host.ip}:{node.port}"
