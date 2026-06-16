"""Device allocation for W3C new-session requests (grid-router spec §3-4).

The service composes existing machinery — capability matching, the device row
lock, the intent reconciler — and owns no writes to protected state columns:
``busy`` is derived from the ``pending`` Session row by the reconciler.
"""

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol, cast

from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import ColumnElement, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession as DbSession
from sqlalchemy.orm import selectinload

from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services.common import build_grid_stereotype_caps
from app.appium_nodes.services.node_viability import device_node_is_viable, node_viable_predicate
from app.core.protocols import SettingsReader
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.intent import IntentService
from app.events.protocols import EventPublisher
from app.grid.constants import RETRY_INTERVAL_SEC
from app.grid.matching import LEGACY_RUN_ID_CAP, CapabilityMergeError, candidate_matches_stereotype, merge_candidates
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.packs.services.capability import StereotypeTemplate, load_stereotype_template
from app.packs.services.start_shim import build_device_context, resolve_pack_for_device
from app.runs import service as run_service
from app.runs.models import TERMINAL_STATES, TestRun
from app.sessions import service as session_service
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_inflight import viability_probe_lock_active

logger = logging.getLogger(__name__)

# Registry default for ``general.session_viability_timeout_sec``, used only when the
# service is constructed without a settings reader (unit tests); production wiring
# always passes one (``composition.py``).
_SESSION_VIABILITY_TIMEOUT_FALLBACK_SEC = 120

GRID_ALLOCATION_OUTCOME_TOTAL = Counter(
    "gridfleet_grid_allocation_outcome",
    "Allocation attempt outcomes for new-session requests.",
    labelnames=("outcome",),  # allocated | queued | invalid | expired | claim_expired
)
GRID_QUEUE_DEPTH = Gauge(
    "gridfleet_grid_queue_depth",
    "Waiting tickets in grid_session_queue.",
)
GRID_ALLOCATION_PROBE_DEFERRED_TOTAL = Counter(
    "gridfleet_grid_allocation_probe_deferred",
    "Device claims skipped because a session-viability probe held the device's probe lock.",
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
    "Total wall-clock time a /internal/grid/allocate long-poll waited before returning, by outcome. "
    "Separates capacity scarcity (queue wait) from try_allocate service time.",
    labelnames=("outcome",),  # allocated | queued
    # The long poll runs to LONG_POLL_SEC (25s); extend past the 10s default ceiling (#9).
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 25.0, 30.0),
)

# Extra budget on top of grid.claim_window_sec before the reaper fails a pending row.
# Covers the router's confirm retries (a confirm whose response was lost re-posts the
# same confirm, which can outlive the create cap): the router-side confirm budget is
# being tightened in parallel to fit inside this grace.
CONFIRM_GRACE_SEC = 60


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
    """Whether moving a ticket from *current* to *to* is a legal transition.

    ``waiting`` is the active source: it advances to ``claimed`` (a device was
    found), ``cancelled`` (invalid body) or ``expired`` (reaper). ``resume_claimed``
    rewinds a terminalized ticket back to ``waiting`` when the client is still
    long-polling but its claimed Session was reaped — and that reaping path
    (``fail`` -> ``expire_tickets_for_session``) moves the ticket ``claimed ->
    expired`` first, so the rewind source is ``claimed`` OR ``expired``.
    ``cancelled`` (invalid body) is a true sink — it is never resumed.
    """
    if current == to:
        return True
    if current == GridQueueStatus.waiting:
        return to in (GridQueueStatus.claimed, GridQueueStatus.cancelled, GridQueueStatus.expired)
    return current in (GridQueueStatus.claimed, GridQueueStatus.expired) and to == GridQueueStatus.waiting


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


# Bulk Core UPDATE paths only ever terminalize a ``claimed`` ticket to ``expired``
# (session ended / orphan reaped). The per-row ``_legal_ticket_transition`` table
# deliberately omits ``claimed -> expired`` — that terminalization is bulk-only — so
# the bulk seam carries its own small legal set rather than widening the per-row table
# (which would also relax what the single-ticket seam permits).
_LEGAL_BULK_TRANSITIONS = frozenset({(GridQueueStatus.claimed, GridQueueStatus.expired)})


def _legal_bulk_ticket_transition(from_status: GridQueueStatus, to: GridQueueStatus) -> bool:
    return (from_status, to) in _LEGAL_BULK_TRANSITIONS


async def transition_tickets_bulk(
    db: DbSession,
    *,
    from_status: GridQueueStatus,
    to: GridQueueStatus,
    reason: str,
    extra_where: Sequence[ColumnElement[bool]] = (),
    synchronize_session: Literal[False, "auto"] = "auto",
) -> int:
    """Bulk counterpart of ``transition_ticket`` for Core ``UPDATE`` paths.

    Enforces a bulk legality table against the statically-known source status
    (the ``status == from_status`` WHERE arm guarantees every transitioned row
    matches it), so bulk terminalization stays inside the single-seam contract
    instead of bypassing it. Returns the number of rows transitioned.
    """
    if not _legal_bulk_ticket_transition(from_status, to):
        raise ValueError(f"illegal bulk ticket transition {from_status} -> {to} (reason={reason})")
    result = await db.execute(
        update(GridSessionQueueTicket)
        .where(GridSessionQueueTicket.status == from_status, *extra_where)
        .values(status=to)
        .execution_options(synchronize_session=synchronize_session)
    )
    count = int(getattr(result, "rowcount", 0) or 0)
    if count:
        logger.debug("grid_ticket_bulk_transition %s->%s count=%d reason=%s", from_status, to, count, reason)
    return count


IntentFactory = Callable[[DbSession], IntentService]
# A per-attempt cache of pack-rendered stereotype templates keyed by (pack_id,
# platform_id). The template half is device-independent (#11), so a fleet of
# same-pack devices renders one DB lookup per unique pack/platform per attempt.
StereotypeTemplateCache = dict[tuple[str, str], StereotypeTemplate]


class StereotypeProvider(Protocol):
    async def __call__(
        self,
        db: DbSession,
        device: Device,
        *,
        template_cache: StereotypeTemplateCache | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AllocationResult:
    allocation_id: uuid.UUID
    target: str


class RunNotActiveError(Exception):
    """A run-bound allocate names a run that is missing or already terminal."""

    def __init__(self, run_id: uuid.UUID, state: str) -> None:
        super().__init__(f"run {run_id} is {state}; sessions can only be created for a live (non-terminal) run")


async def expire_tickets_for_session(db: DbSession, session_row_id: uuid.UUID) -> int:
    """Terminalize any ``claimed`` ticket still pointing at *session_row_id*.

    A ticket goes ``claimed`` when ``_claim`` mints its pending Session row, but it
    is never moved off ``claimed`` afterwards: when the allocation finishes (failed
    by the reaper, ended by the router, or swept closed) the ticket is left
    dangling. Once ``data_cleanup`` purges the Session the FK (``ondelete=SET NULL``)
    nulls ``session_row_id`` and the junk ticket lives forever (harness G7).

    Called from every seam where an allocation Session leaves running/pending:
    ``AllocationService.fail`` (reaper) and ``close_running_session`` (router DELETE
    + session_sync sweep). Idempotent — the ``status='claimed'`` guard makes a second
    call a no-op. Returns the number of tickets transitioned.
    """
    # Bulk claimed -> expired through the guarded bulk seam (transition_tickets_bulk).
    return await transition_tickets_bulk(
        db,
        from_status=GridQueueStatus.claimed,
        to=GridQueueStatus.expired,
        reason="session_ended",
        extra_where=(GridSessionQueueTicket.session_row_id == session_row_id,),
    )


def _ticket_passes_reservation(ticket_run_id: uuid.UUID | None, reservation_run_id: uuid.UUID | None) -> bool:
    """Strict symmetric admission (run-scoped-endpoint spec §1/§3): a run-bound
    ticket may take only devices reserved for its run; a free ticket may take
    only unreserved devices. No spillover in either direction."""
    return ticket_run_id == reservation_run_id


def _candidate_can_take(
    candidate: dict[str, Any],
    stereotype: dict[str, Any],
    ticket_run_id: uuid.UUID | None,
    reservation_run_id: uuid.UUID | None,
) -> bool:
    """Shared two-step gate (harness Q14): does *candidate* match the device's
    *stereotype* AND does its ticket clear the device's reservation state? Used
    by both ``try_allocate`` (to claim) and ``_older_waiter_blocks`` (the FIFO
    veto), which must apply identical admission rules."""
    if not candidate_matches_stereotype(candidate, stereotype):
        return False
    return _ticket_passes_reservation(ticket_run_id, reservation_run_id)


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

    async def confirm(
        self,
        db: DbSession,
        *,
        allocation_id: uuid.UUID,
        appium_session_id: str,
        appium_capabilities: dict[str, Any] | None = None,
    ) -> None:
        """Swap the placeholder session id for the Appium id and promote to ``running``.

        The status transition is a conditional UPDATE guarded on ``status='pending'``
        so the reaper failing the row mid-confirm loses the race deterministically:
        rowcount 0 means the row is no longer pending. Before raising we check for the
        lost-response retry case: a first confirm committed, its response was lost, and
        the router retried the same confirm. If the row is already ``running`` with the
        SAME ``appium_session_id`` we return success (idempotent). Any other state — a
        different id, or a row failed/reaped — is a genuine conflict and still raises
        (the router rolls back the Appium session via 409).

        ``last_activity_at`` is intentionally NOT stamped at confirm: a ``running``
        row with NULL activity means "the client never issued a command". The
        router's server-stamped ``/internal/grid/activity`` flush is the only
        writer, and ``SessionSyncService._check_liveness`` reaps a never-commanded
        session after ``grid.session_first_command_grace_sec`` (measured from the
        claim-time ``started_at``).
        """
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
            # A running row already carries this Appium session id (the partial unique
            # ux_sessions_session_id_running) — e.g. the legacy register_session API
            # inserted running(X) for the same session while this alloc row still held
            # its 'alloc-<uuid>' placeholder. That is exactly the conflict the 409 path
            # exists for: roll the failed UPDATE back (it left the transaction poisoned)
            # and surface it as not-pending so the router rolls back the Appium session
            # via 409 — never as an unhandled 500 that wedges the allocation.
            await db.rollback()
            raise AllocationNotPendingError(allocation_id) from None
        if int(getattr(result, "rowcount", 0) or 0) == 0:
            # Idempotent retry: a first confirm committed but its response was lost, so
            # the router resent the same confirm. Accept it iff the row is already
            # running with the same Appium id; otherwise it is a real conflict (409).
            existing_session_id = await db.scalar(
                select(Session.session_id).where(
                    Session.id == allocation_id,
                    Session.status == SessionStatus.running,
                )
            )
            if existing_session_id != appium_session_id:
                raise AllocationNotPendingError(allocation_id)
            # The first confirm already promoted the row and emitted session.started;
            # the retry is a no-op success and must not re-emit the event.
            return
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
        # row was already confirmed/reaped, so we no-op (idempotent) and skip reconcile.
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
            return  # idempotent: already confirmed/reaped
        await expire_tickets_for_session(db, allocation_id)
        await db.refresh(row)
        await db.flush()
        if device_id is not None:
            intent = self._intent_factory(db)
            await intent.mark_dirty_and_reconcile(device_id, reason="grid_allocation_failed", publisher=self._publisher)

    async def record_doomed_appium_session(
        self, db: DbSession, *, allocation_id: uuid.UUID, appium_session_id: str
    ) -> bool:
        """Stamp the Appium id reported by a 409-rejected confirm onto the terminal row.

        When a confirm loses to the reaper/run-cancel, the router rolls the
        freshly-created Appium session back with a best-effort DELETE. If that DELETE
        fails, nothing tracks the orphan — and the orphan sweep spares unknown ids on
        a device holding a new pending row (it cannot tell an in-creation session
        from an orphan by id). Swapping the terminal row's ``alloc-`` placeholder for
        the real id makes the orphan a *known doomed id* the sweep can kill precisely.

        Guards: only a terminal row still carrying its placeholder is stamped, and
        never while any live row owns the id (the legacy-register conflict case —
        that session is alive and tracked, not an orphan). Returns True iff stamped.
        """
        row = await db.get(Session, allocation_id)
        if row is None or row.ended_at is None or not row.session_id.startswith("alloc-"):
            return False
        live_owner = await db.scalar(
            select(Session.id).where(Session.session_id == appium_session_id, live_session_predicate()).limit(1)
        )
        if live_owner is not None:
            return False
        row.session_id = appium_session_id
        await db.flush()
        logger.info(
            "grid_doomed_appium_session_recorded allocation=%s appium_session=%s device=%s",
            allocation_id,
            appium_session_id,
            row.device_id,
        )
        return True

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
            Session.started_at < now - timedelta(seconds=claim_window + CONFIRM_GRACE_SEC),
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
        return {"pending_failed": pending_failed, "tickets_expired": tickets_expired}

    async def reap_orphaned_claims(self, db: DbSession) -> int:
        """Expire ``claimed`` tickets whose client has abandoned them AND whose Session can
        never become live again.

        Defense-in-depth behind ``expire_tickets_for_session`` (which terminalizes a claim
        synchronously when its Session ends). This sweep clears residue that a missed seam or
        an older build leaked: ``claimed`` tickets left pointing at an ended/purged Session
        that nothing else moves off ``claimed`` (harness G7). Both gates are required:

        * **abandoned** — ``last_polled_at`` older than the liveness window. A still-polling
          client is left for ``resume_claimed`` to rewind to ``waiting`` on its next poll, so
          the sweep never races an honest resume.
        * **not live** — ``session_row_id`` IS NULL (Session purged → FK ``SET NULL``), OR the
          Session is missing / ended / not ``pending``|``running`` (the same predicate
          ``resume_claimed`` treats as un-resumable).

        An honest in-flight claim is spared by the not-live gate (its Session is
        ``pending``/``running``). Returns the number of tickets transitioned.
        """
        stale_cutoff = _ticket_liveness_cutoff(now_utc())
        # A Session that can never re-serve a claim: ended, or not pending/running. (A purged
        # Session nulls session_row_id via FK SET NULL — caught by the IS NULL arm below — so a
        # dangling FK to a missing row does not occur.)
        not_live_session_ids = select(Session.id).where(
            or_(
                Session.ended_at.is_not(None),
                Session.status.not_in((SessionStatus.pending, SessionStatus.running)),
            )
        )
        # Bulk claimed -> expired through the guarded bulk seam (transition_tickets_bulk),
        # mirroring expire_tickets_for_session.
        reaped = await transition_tickets_bulk(
            db,
            from_status=GridQueueStatus.claimed,
            to=GridQueueStatus.expired,
            reason="orphan_claim_reaped",
            extra_where=(
                GridSessionQueueTicket.last_polled_at < stale_cutoff,
                or_(
                    GridSessionQueueTicket.session_row_id.is_(None),
                    GridSessionQueueTicket.session_row_id.in_(not_live_session_ids),
                ),
            ),
            synchronize_session=False,
        )
        if reaped:
            GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="orphan_claim_reaped").inc(reaped)
        await db.flush()
        return reaped

    async def try_allocate(self, db: DbSession, *, ticket: GridSessionQueueTicket) -> AllocationResult | None:
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
        if ticket.run_id is not None:
            run = await run_service.get_run(db, ticket.run_id)
            if run is None or run.state in TERMINAL_STATES:
                state = run.state.value if run is not None else "missing"
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
        # Clean-break tombstone (spec §1): reject cap-era clients loudly.
        if any(LEGACY_RUN_ID_CAP in c for c in candidates):
            transition_ticket(ticket, GridQueueStatus.cancelled, reason="legacy_run_id_cap")
            GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="invalid").inc()
            raise CapabilityMergeError(
                "the gridfleet:run_id capability is no longer supported; "
                "create run sessions through the router's /run/{run_id} endpoint"
            )
        # Hoist the older-waiter load + per-ticket candidate merge out of the
        # per-device x per-candidate loops: load once, pre-merge once, reuse.
        older_candidate_sets = await self._older_waiter_candidate_sets(db, ticket)
        eligible = await self._eligible_devices(db)
        # Batch the reservation load for every eligible device once instead of one
        # SELECT per device per long-poll tick (#11).
        reservation_map = await run_service.get_device_reservation_map(db, [d.id for d in eligible])
        # Memoize the pack-rendered stereotype per device within this attempt: the
        # render hits the DB per device, and the device loop below may re-touch a
        # device. The interpolated result is per-device (udid, os_version) so it is
        # NOT poolable across same-pack devices. The DB-touching half — the pack
        # template — IS device-independent, so it is cached separately by
        # (pack_id, platform_id) within this attempt, collapsing N same-pack DB
        # lookups to one (#11). Both caches are per-attempt; stereotypes follow pack
        # releases so cross-tick caching is avoided (#13).
        stereotype_cache: dict[uuid.UUID, dict[str, Any]] = {}
        template_cache: StereotypeTemplateCache = {}
        for device in eligible:
            stereotype = stereotype_cache.get(device.id)
            if stereotype is None:
                stereotype = await self._stereotype_provider(db, device, template_cache=template_cache)
                stereotype_cache[device.id] = stereotype
            reservation_run_id = self._reservation_run_id(reservation_map.get(device.id), device.id)
            for candidate in candidates:
                if not _candidate_can_take(candidate, stereotype, ticket.run_id, reservation_run_id):
                    continue
                # FIFO veto, reservation-aware: only count older waiters that could
                # actually take THIS device — i.e. whose ticket clears the same
                # reservation gate and whose candidate matches the stereotype.
                if self._older_waiter_blocks(older_candidate_sets, stereotype, reservation_run_id):
                    continue
                result = await self._claim(db, ticket=ticket, device=device, candidate=candidate, run_id=ticket.run_id)
                if result is not None:
                    GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="allocated").inc()
                    return result
        return None

    async def resume_claimed(self, db: DbSession, *, ticket: GridSessionQueueTicket) -> AllocationResult | None:
        """Idempotently resume a ``claimed`` ticket whose Allocated response was lost.

        A router retry after a transport error on a committed Allocated response
        re-hits allocate with the same ``claimed`` ticket. Re-claiming would orphan
        the first pending session and double-allocate a device. Instead:

        * If the ticket's Session row is still ``pending`` or ``running`` (not ended),
          return the SAME allocation — the original claim is honest and still alive.
        * If the row was failed/reaped (the claim window expired while the response was
          lost), reset the ticket to ``waiting`` so the caller proceeds to a fresh
          ``try_allocate``. The client is still long-polling; that's the honest
          continuation.
        """
        if ticket.session_row_id is None:
            transition_ticket(ticket, GridQueueStatus.waiting, reason="resume_no_session_row")
            return None
        stmt = (
            select(Session)
            .options(selectinload(Session.device).selectinload(Device.appium_node))
            .options(selectinload(Session.device).selectinload(Device.host))
            .where(Session.id == ticket.session_row_id)
        )
        row = (await db.execute(stmt)).scalars().first()
        if (
            row is None
            or row.ended_at is not None
            or row.status not in (SessionStatus.pending, SessionStatus.running)
            or row.device is None
        ):
            transition_ticket(ticket, GridQueueStatus.waiting, reason="resume_session_reaped")
            return None
        target = resolve_router_target(row)
        if target is None:
            # The device lost its node/host association and no target was ever stored;
            # treat like a reaped claim and let the client wait for a fresh allocation
            # rather than hand back a dead target.
            transition_ticket(ticket, GridQueueStatus.waiting, reason="resume_no_target")
            return None
        return AllocationResult(allocation_id=row.id, target=target)

    async def _eligible_devices(self, db: DbSession) -> list[Device]:
        stmt = (
            select(Device)
            .outerjoin(AppiumNode, AppiumNode.device_id == Device.id)
            .where(Device.operational_state == DeviceOperationalState.available)
            .where(node_viable_predicate())
            .where(~select(Session.id).where(Session.device_id == Device.id, live_session_predicate()).exists())
        )
        devices = list((await db.execute(stmt)).scalars().all())
        GRID_ELIGIBLE_DEVICES.set(len(devices))
        return devices

    @staticmethod
    def _reservation_run_id(reservation_run: TestRun | None, device_id: uuid.UUID) -> uuid.UUID | None:
        """Return the reservation's run id for *device_id*, or ``None`` if the
        device carries no admitting reservation (open to any ticket).

        Pure projection over the run loaded once by ``get_device_reservation_map``: a
        live (non-terminal), non-excluded reservation gates the device to its owning
        run (spec §3). The reservation is honoured from run creation (`preparing`)
        onward — not only once `active` — so the run can take ITS reserved devices
        during preparation and free tickets cannot steal them in that window.
        Anything else (no reservation, terminal run, excluded entry) leaves it
        unreserved.
        """
        if reservation_run is None or reservation_run.state in TERMINAL_STATES:
            return None
        entry = run_service.get_reservation_entry_for_device(reservation_run, device_id)
        if run_service.reservation_entry_is_excluded(entry):
            return None
        return reservation_run.id

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
                sets.append((older.run_id, merge_candidates(older.requested_body)))
            except CapabilityMergeError:
                continue
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

    async def _claim(
        self,
        db: DbSession,
        *,
        ticket: GridSessionQueueTicket,
        device: Device,
        candidate: dict[str, Any],
        run_id: uuid.UUID | None,
    ) -> AllocationResult | None:
        # A session-viability probe is a REAL Appium session on an ``available``
        # device, posted directly to the node — no Session row exists until the
        # probe completes, so the live-session recheck below cannot see it. Its
        # only allocation-visible footprint is the control-plane probe lock.
        # Claiming mid-probe races the probe's uia2 startup for the device's
        # static systemPort and fails the client create ("local port #8200 is
        # busy", proven live 2026-06-07). Skip the device for this tick — the
        # ticket stays waiting and retries on its next poll. Checked BEFORE the
        # row lock (DEBT-2): the read costs a DB round trip and must not extend
        # the lock hold; the probe's own staleness rule tolerates the tiny
        # unlocked-check-to-locked-claim race (one failed create + retry, same
        # exposure as the _eligible_devices snapshot).
        viability_timeout_sec = (
            int(cast("int", self._settings.get("general.session_viability_timeout_sec")))
            if self._settings is not None
            else _SESSION_VIABILITY_TIMEOUT_FALLBACK_SEC
        )
        if await viability_probe_lock_active(db, device.id, timeout_sec=viability_timeout_sec):
            GRID_ALLOCATION_PROBE_DEFERRED_TOTAL.inc()
            logger.info(
                "grid_allocation_deferred_probe_inflight device=%s ticket=%s",
                device.id,
                ticket.id,
            )
            return None
        locked = await device_locking.lock_device(db, device.id)
        # Re-verify under the row lock: state, node viability, and absence of active
        # sessions may have changed since _eligible_devices ran.
        if locked.operational_state != DeviceOperationalState.available:
            return None
        if not device_node_is_viable(locked):
            return None
        recheck = await db.execute(select(Session.id).where(live_session_predicate(locked.id)))
        if recheck.first() is not None:
            return None
        target = node_target(locked)
        if target is None:
            # An `available` device with no node/host association is broken host/agent
            # state: the ticket keeps waiting while the device looks claimable.
            logger.warning(
                "grid_allocation_no_node_target device=%s ticket=%s (appium_node=%s host=%s)",
                locked.id,
                ticket.id,
                locked.appium_node is not None,
                locked.host is not None,
            )
            return None
        # Surface the client's test label in the Session.test_name column (the Sessions UI
        # reads it). The legacy register_session API took it as an explicit field; the
        # router/grid flow that replaced it must lift it from the requested caps here.
        requested_test_name = candidate.get("gridfleet:testName")
        row = Session(
            id=uuid.uuid4(),
            session_id=f"alloc-{uuid.uuid4()}",  # placeholder until confirm; unique, never 'running'
            device_id=locked.id,
            status=SessionStatus.pending,
            requested_capabilities=candidate,
            test_name=requested_test_name if isinstance(requested_test_name, str) else None,
            run_id=run_id,
            # Persist the allocation target so /routes can fall back to it if the
            # device's node port is transiently stale-cleared later (#6).
            router_target=target,
        )
        db.add(row)
        # Flush the Session row before pointing the ticket at it: there is no ORM
        # relationship between the two mappers, so the unit of work would not
        # order the INSERT before the FK-bearing UPDATE on its own.
        await db.flush()
        transition_ticket(ticket, GridQueueStatus.claimed, reason="device_claimed")
        ticket.session_row_id = row.id
        await db.flush()
        intent = self._intent_factory(db)
        await intent.mark_dirty_and_reconcile(locked.id, reason="grid_allocation_pending", publisher=self._publisher)
        return AllocationResult(allocation_id=row.id, target=target)


async def pack_slot_stereotype(
    db: DbSession,
    device: Device,
    *,
    template_cache: StereotypeTemplateCache | None = None,
) -> dict[str, Any]:
    """Compose the slot stereotype the relay advertises for *device*.

    Mirrors what ``start_remote_node`` sends to the agent: pack-rendered
    stereotype (platformName, automationName, manifest filters, ``appium:udid``
    via device context) merged with the manager-owned routing surface
    (deviceId + tag fanout) from ``build_grid_stereotype_caps``.

    When the device's pack/platform cannot be resolved (pack deleted, platform
    dropped from the release) the pack half falls back to empty so one broken pack
    cannot wedge allocation for every other device — but the failure is logged and
    counted (``gridfleet_grid_stereotype_lookup_error``) because such a device
    advertises no capabilities and is silently unmatchable until repaired (#1).

    *template_cache*, when supplied, memoizes the device-independent template by
    ``(pack_id, platform_id)`` so a fleet of same-pack devices issues one DB
    lookup per unique pack/platform instead of one per device (#11).
    """
    stereotype: dict[str, Any] = {}
    resolved = resolve_pack_for_device(device)
    if resolved is not None:
        pack_id, platform_id = resolved
        try:
            template = template_cache.get(resolved) if template_cache is not None else None
            if template is None:
                template = await load_stereotype_template(db, pack_id=pack_id, platform_id=platform_id)
                if template_cache is not None:
                    template_cache[resolved] = template
            stereotype = template.interpolate(build_device_context(device))
        except LookupError as exc:
            GRID_STEREOTYPE_LOOKUP_ERROR_TOTAL.inc()
            logger.warning(
                "grid_stereotype_lookup_error device=%s pack=%s platform=%s: %s",
                device.id,
                pack_id,
                platform_id,
                exc,
            )
            stereotype = {}
    stereotype.update(build_grid_stereotype_caps(device, pack_stereotype=None))
    return stereotype


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
