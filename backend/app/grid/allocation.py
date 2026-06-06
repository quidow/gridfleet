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
from sqlalchemy.orm.util import identity_key

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
from app.runs.models import RunState
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
        rowcount 0 means the row is no longer pending and we raise (the router rolls
        back the Appium session).
        """
        result = await db.execute(
            update(Session)
            .where(Session.id == allocation_id, Session.status == SessionStatus.pending)
            .values(session_id=appium_session_id, status=SessionStatus.running)
        )
        if int(getattr(result, "rowcount", 0) or 0) == 0:
            raise AllocationNotPendingError(allocation_id)
        # Keep any ORM-mapped instance coherent with the Core write.
        row = db.identity_map.get(identity_key(Session, allocation_id), None)
        if row is not None:
            await db.refresh(row)
        await db.flush()

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
            Session.started_at < now - timedelta(seconds=claim_window),
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
        eligible = await self._eligible_devices(db)
        for device in eligible:
            stereotype = await self._stereotype_provider(db, device)
            for candidate in candidates:
                if not candidate_matches_stereotype(candidate, stereotype):
                    continue
                allowed, run_id = await self._reservation_gate(db, device, candidate)
                if not allowed:
                    continue
                if await self._older_waiter_matches(db, ticket, stereotype):
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
        target = node_target(row.device)
        if target is None:
            # The device lost its node/host association; treat like a reaped claim and
            # let the client wait for a fresh allocation rather than hand back a dead target.
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

    async def _reservation_gate(
        self, db: DbSession, device: Device, candidate: dict[str, Any]
    ) -> tuple[bool, uuid.UUID | None]:
        """Return ``(allowed, run_id_to_associate)``.

        An active reservation admits only the owning run's sessions (spec §3):
        the candidate must present the run's id in ``gridfleet:run_id``.
        """
        reservation_run, reservation_entry = await run_service.get_device_reservation_with_entry(db, device.id)
        if (
            reservation_run is None
            or reservation_run.state != RunState.active
            or run_service.reservation_entry_is_excluded(reservation_entry)
        ):
            return True, None
        if candidate.get(RUN_ID_CAP) == str(reservation_run.id):
            return True, reservation_run.id
        return False, None

    async def _older_waiter_matches(
        self, db: DbSession, ticket: GridSessionQueueTicket, stereotype: dict[str, Any]
    ) -> bool:
        # O(older waiting tickets x their firstMatch count) per allocation attempt.
        # Deliberately unbounded: any LIMIT would let a younger ticket jump tickets
        # beyond the cap, breaking the FIFO invariant. Queue depth is naturally
        # bounded by grid.queue_timeout_sec reaping; revisit only if queue depth
        # metrics ever show this dominating allocation latency.
        stmt = (
            select(GridSessionQueueTicket)
            .where(
                GridSessionQueueTicket.status == GridQueueStatus.waiting,
                GridSessionQueueTicket.created_at < ticket.created_at,
            )
            .order_by(GridSessionQueueTicket.created_at)
        )
        for older in (await db.execute(stmt)).scalars():
            try:
                older_candidates = merge_candidates(older.requested_body)
            except CapabilityMergeError:
                continue
            if any(candidate_matches_stereotype(c, stereotype) for c in older_candidates):
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
