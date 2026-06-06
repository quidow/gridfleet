"""Device allocation for W3C new-session requests (grid-router spec §3-4).

The service composes existing machinery — capability matching, the device row
lock, the intent reconciler — and owns no writes to protected state columns:
``busy`` is derived from the ``pending`` Session row by the reconciler.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from prometheus_client import Counter, Gauge
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession as DbSession
from sqlalchemy.orm import selectinload

from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services.common import build_grid_stereotype_caps
from app.appium_nodes.services.node_viability import device_node_is_viable, node_viable_predicate
from app.core.protocols import SettingsReader
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.intent import IntentService
from app.events.protocols import EventPublisher
from app.grid.matching import RUN_ID_CAP, CapabilityMergeError, candidate_matches_stereotype, merge_candidates
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.packs.services.capability import render_stereotype
from app.packs.services.start_shim import build_device_context, resolve_pack_for_device
from app.runs import service as run_service
from app.runs.models import RunState, TestRun
from app.sessions import service as session_service
from app.sessions.models import Session, SessionStatus

logger = logging.getLogger(__name__)

GRID_ALLOCATION_OUTCOME_TOTAL = Counter(
    "gridfleet_grid_allocation_outcome",
    "Allocation attempt outcomes for new-session requests.",
    labelnames=("outcome",),  # allocated | queued | invalid | expired | claim_expired
)
GRID_QUEUE_DEPTH = Gauge(
    "gridfleet_grid_queue_depth",
    "Waiting tickets in grid_session_queue.",
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


IntentFactory = Callable[[DbSession], IntentService]
StereotypeProvider = Callable[[DbSession, Device], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class AllocationResult:
    allocation_id: uuid.UUID
    target: str


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
    result = await db.execute(
        update(GridSessionQueueTicket)
        .where(
            GridSessionQueueTicket.session_row_id == session_row_id,
            GridSessionQueueTicket.status == GridQueueStatus.claimed,
        )
        .values(status=GridQueueStatus.expired)
    )
    return int(getattr(result, "rowcount", 0) or 0)


def _candidate_passes_reservation(
    candidate: dict[str, Any], reservation_run_id: uuid.UUID | None
) -> tuple[bool, uuid.UUID | None]:
    """Reservation gate for one candidate against a device's reservation state.

    ``reservation_run_id`` is the device's admitting reservation run (``None`` when
    unreserved). An unreserved device admits any candidate. A reserved device
    admits only the owning run's sessions (spec §3): the candidate must present
    the run's id in ``gridfleet:run_id``. Returns ``(allowed, run_id_to_associate)``.
    """
    if reservation_run_id is None:
        return True, None
    if candidate.get(RUN_ID_CAP) == str(reservation_run_id):
        return True, reservation_run_id
    return False, None


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

    async def confirm(self, db: DbSession, *, allocation_id: uuid.UUID, appium_session_id: str) -> None:
        """Swap the placeholder session id for the Appium id and promote to ``running``.

        The status transition is a conditional UPDATE guarded on ``status='pending'``
        so the reaper failing the row mid-confirm loses the race deterministically:
        rowcount 0 means the row is no longer pending. Before raising we check for the
        lost-response retry case: a first confirm committed, its response was lost, and
        the router retried the same confirm. If the row is already ``running`` with the
        SAME ``appium_session_id`` we return success (idempotent). Any other state — a
        different id, or a row failed/reaped — is a genuine conflict and still raises
        (the router rolls back the Appium session via 409).

        ``last_activity_at`` is stamped at confirm so the idle clock starts when the
        session becomes live, not at the claim-time ``started_at`` (which precedes a
        possibly multi-minute Appium create).
        """
        result = await db.execute(
            update(Session)
            .where(Session.id == allocation_id, Session.status == SessionStatus.pending)
            .values(
                session_id=appium_session_id,
                status=SessionStatus.running,
                last_activity_at=datetime.now(UTC),
            )
        )
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
        await db.flush()
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
                ended_at=datetime.now(UTC),
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
        now = datetime.now(UTC)

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

        tickets_stmt = select(GridSessionQueueTicket).where(
            GridSessionQueueTicket.status == GridQueueStatus.waiting,
            GridSessionQueueTicket.created_at < now - timedelta(seconds=queue_timeout),
        )
        tickets_expired = 0
        for stale in (await db.execute(tickets_stmt)).scalars():
            stale.status = GridQueueStatus.expired
            GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="expired").inc()
            tickets_expired += 1
        await db.flush()
        return {"pending_failed": pending_failed, "tickets_expired": tickets_expired}

    async def try_allocate(self, db: DbSession, *, ticket: GridSessionQueueTicket) -> AllocationResult | None:
        try:
            candidates = merge_candidates(ticket.requested_body)
        except CapabilityMergeError:
            logger.warning("grid_allocation_invalid_body ticket=%s", ticket.id)
            ticket.status = GridQueueStatus.cancelled
            GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="invalid").inc()
            return None
        # Hoist the older-waiter load + per-ticket candidate merge out of the
        # per-device x per-candidate loops: load once, pre-merge once, reuse.
        older_candidate_sets = await self._older_waiter_candidate_sets(db, ticket)
        eligible = await self._eligible_devices(db)
        # Batch the reservation load for every eligible device once instead of one
        # SELECT per device per long-poll tick (#11).
        reservation_map = await run_service.get_device_reservation_map(db, [d.id for d in eligible])
        # Memoize the pack-rendered stereotype per device within this attempt: the
        # render hits the DB per device, and the device loop below may re-touch a
        # device. The render interpolates per-device context (udid, os_version), so it
        # is NOT poolable across same-pack devices; cross-tick caching is also avoided —
        # stereotypes follow pack releases (#13).
        stereotype_cache: dict[uuid.UUID, dict[str, Any]] = {}
        for device in eligible:
            stereotype = stereotype_cache.get(device.id)
            if stereotype is None:
                stereotype = await self._stereotype_provider(db, device)
                stereotype_cache[device.id] = stereotype
            reservation_run_id = self._reservation_run_id(reservation_map.get(device.id), device.id)
            for candidate in candidates:
                if not candidate_matches_stereotype(candidate, stereotype):
                    continue
                allowed, run_id = _candidate_passes_reservation(candidate, reservation_run_id)
                if not allowed:
                    continue
                # FIFO veto, reservation-aware: only count older waiters that could
                # actually take THIS device — i.e. whose candidate matches the
                # stereotype AND clears the same reservation gate. An older
                # run-less waiter cannot block this device when it is reserved.
                if self._older_waiter_blocks(older_candidate_sets, stereotype, reservation_run_id):
                    continue
                result = await self._claim(db, ticket=ticket, device=device, candidate=candidate, run_id=run_id)
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
            ticket.status = GridQueueStatus.waiting
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
            ticket.status = GridQueueStatus.waiting
            return None
        target = resolve_router_target(row)
        if target is None:
            # The device lost its node/host association and no target was ever stored;
            # treat like a reaped claim and let the client wait for a fresh allocation
            # rather than hand back a dead target.
            ticket.status = GridQueueStatus.waiting
            return None
        return AllocationResult(allocation_id=row.id, target=target)

    async def _eligible_devices(self, db: DbSession) -> list[Device]:
        stmt = (
            select(Device)
            .outerjoin(AppiumNode, AppiumNode.device_id == Device.id)
            .where(Device.operational_state == DeviceOperationalState.available)
            .where(node_viable_predicate())
            .where(
                ~select(Session.id)
                .where(
                    Session.device_id == Device.id,
                    Session.status.in_((SessionStatus.running, SessionStatus.pending)),
                    Session.ended_at.is_(None),
                )
                .exists()
            )
        )
        return list((await db.execute(stmt)).scalars().all())

    @staticmethod
    def _reservation_run_id(reservation_run: TestRun | None, device_id: uuid.UUID) -> uuid.UUID | None:
        """Return the active reservation's run id for *device_id*, or ``None`` if the
        device carries no admitting reservation (open to any ticket).

        Pure projection over the run loaded once by ``get_device_reservation_map``: an
        active, non-excluded reservation gates the device to its owning run (spec §3);
        anything else (no reservation, non-active run, excluded entry) leaves it
        unreserved.
        """
        if reservation_run is None or reservation_run.state != RunState.active:
            return None
        entry = run_service.get_reservation_entry_for_device(reservation_run, device_id)
        if run_service.reservation_entry_is_excluded(entry):
            return None
        return reservation_run.id

    async def _older_waiter_candidate_sets(
        self, db: DbSession, ticket: GridSessionQueueTicket
    ) -> list[list[dict[str, Any]]]:
        """Pre-merge the firstMatch candidates of every older waiting ticket once.

        Computed once per ``try_allocate`` call (not per device x candidate) and
        reused across the device loop. O(older waiting tickets x firstMatch count)
        is deliberately unbounded — queue depth is bounded by
        grid.queue_timeout_sec reaping; revisit only if metrics show it dominating.
        Tickets with an invalid body are dropped (they cannot block anyone).
        """
        stmt = (
            select(GridSessionQueueTicket)
            .where(
                GridSessionQueueTicket.status == GridQueueStatus.waiting,
                GridSessionQueueTicket.created_at < ticket.created_at,
            )
            .order_by(GridSessionQueueTicket.created_at)
        )
        sets: list[list[dict[str, Any]]] = []
        for older in (await db.execute(stmt)).scalars():
            try:
                sets.append(merge_candidates(older.requested_body))
            except CapabilityMergeError:
                continue
        return sets

    @staticmethod
    def _older_waiter_blocks(
        older_candidate_sets: list[list[dict[str, Any]]],
        stereotype: dict[str, Any],
        reservation_run_id: uuid.UUID | None,
    ) -> bool:
        """FIFO veto: does any older waiter have a candidate that could take this
        device? Reservation-aware — an older candidate counts only if it both
        matches the stereotype AND clears the device's reservation gate, so a
        run-less older waiter never blocks a younger run-owned ticket on a reserved
        device.
        """
        for older_candidates in older_candidate_sets:
            for c in older_candidates:
                if not candidate_matches_stereotype(c, stereotype):
                    continue
                allowed, _ = _candidate_passes_reservation(c, reservation_run_id)
                if allowed:
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
        locked = await device_locking.lock_device(db, device.id)
        # Re-verify under the row lock: state, node viability, and absence of active
        # sessions may have changed since _eligible_devices ran.
        if locked.operational_state != DeviceOperationalState.available:
            return None
        if not device_node_is_viable(locked):
            return None
        recheck = await db.execute(
            select(Session.id).where(
                Session.device_id == locked.id,
                Session.status.in_((SessionStatus.running, SessionStatus.pending)),
                Session.ended_at.is_(None),
            )
        )
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
        row = Session(
            id=uuid.uuid4(),
            session_id=f"alloc-{uuid.uuid4()}",  # placeholder until confirm; unique, never 'running'
            device_id=locked.id,
            status=SessionStatus.pending,
            requested_capabilities=candidate,
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
        ticket.status = GridQueueStatus.claimed
        ticket.session_row_id = row.id
        await db.flush()
        intent = self._intent_factory(db)
        await intent.mark_dirty_and_reconcile(locked.id, reason="grid_allocation_pending", publisher=self._publisher)
        return AllocationResult(allocation_id=row.id, target=target)


async def pack_slot_stereotype(db: DbSession, device: Device) -> dict[str, Any]:
    """Compose the slot stereotype the relay advertises for *device*.

    Mirrors what ``start_remote_node`` sends to the agent: pack-rendered
    stereotype (platformName, automationName, manifest filters, ``appium:udid``
    via device context) merged with the manager-owned routing surface
    (deviceId + tag fanout) from ``build_grid_stereotype_caps``.
    """
    stereotype: dict[str, Any] = {}
    resolved = resolve_pack_for_device(device)
    if resolved is not None:
        try:
            stereotype = await render_stereotype(
                db, pack_id=resolved[0], platform_id=resolved[1], device_context=build_device_context(device)
            )
        except LookupError:
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

    ``AppiumNode.port`` is the agent-reported Appium server port (the agent's
    ``running_nodes[*].port``), NOT the grid relay's node port.

    ``lock_device`` eager-loads ``appium_node`` and ``host``. Host address uses
    ``host.ip`` — the same expression node registration uses (reconciler_agent).
    """
    node = device.appium_node
    if node is None or node.port is None or device.host is None:
        return None
    return f"http://{device.host.ip}:{node.port}"
