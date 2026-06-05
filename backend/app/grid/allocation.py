"""Device allocation for W3C new-session requests (grid-router spec §3-4).

The service composes existing machinery — capability matching, the device row
lock, the intent reconciler — and owns no writes to protected state columns:
``busy`` is derived from the ``pending`` Session row by the reconciler.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as DbSession

from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.intent import IntentService
from app.events.protocols import EventPublisher
from app.grid.matching import RUN_ID_CAP, CapabilityMergeError, candidate_matches_stereotype, merge_candidates
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.runs import service as run_service
from app.runs.models import RunState
from app.sessions.models import Session, SessionStatus

logger = logging.getLogger(__name__)

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
    ) -> None:
        self._intent_factory = intent_factory
        self._publisher = publisher
        self._stereotype_provider = stereotype_provider

    async def try_allocate(self, db: DbSession, *, ticket: GridSessionQueueTicket) -> AllocationResult | None:
        try:
            candidates = merge_candidates(ticket.requested_body)
        except CapabilityMergeError:
            logger.warning("grid_allocation_invalid_body ticket=%s", ticket.id)
            ticket.status = GridQueueStatus.cancelled
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
                    return result
        return None

    async def _eligible_devices(self, db: DbSession) -> list[Device]:
        stmt = (
            select(Device)
            .where(Device.operational_state == DeviceOperationalState.available)
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
        # Re-verify under the row lock: state and absence of active sessions may have changed.
        if locked.operational_state != DeviceOperationalState.available:
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
        target = _node_target(locked)
        if target is None:
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


def _node_target(device: Device) -> str | None:
    """Direct Appium-node base URL: host address + relay node port.

    ``lock_device`` eager-loads ``appium_node`` and ``host``. Host address uses
    ``host.ip`` — the same expression node registration uses (reconciler_agent).
    """
    node = device.appium_node
    if node is None or node.port is None or device.host is None:
        return None
    return f"http://{device.host.ip}:{node.port}"
