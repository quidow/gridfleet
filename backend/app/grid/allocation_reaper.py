"""Leader-owned loop that expires stale allocations and queue tickets (spec §7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.core.background_loop import BackgroundLoop
from app.core.observability import get_logger
from app.grid.allocation import GRID_QUEUE_DEPTH
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.sessions.service_sync import request_session_sync_wake

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SessionFactory
    from app.grid.services_container import GridServices

logger = get_logger(__name__)
LOOP_NAME = "grid_allocation_reaper"
INTERVAL_SEC = 5.0


class GridAllocationReaperLoop(BackgroundLoop):
    loop_name = LOOP_NAME
    exit_on_leadership_lost = True
    cycle_failed_message = "Grid allocation reaper cycle failed"

    def __init__(self, *, services: GridServices) -> None:
        self._services = services

    @property
    def _session_factory(self) -> SessionFactory:
        return self._services.session_factory

    def _interval(self) -> float:
        return float(INTERVAL_SEC)

    async def _run_cycle(self, db: AsyncSession) -> None:
        await self.run_cycle(db)

    async def run_cycle(self, db: AsyncSession) -> None:
        reaped = await self._services.allocation.reap_expired(db)
        if reaped["pending_failed"] or reaped["tickets_expired"]:
            logger.info(
                "grid_allocation_reaped",
                pending_failed=reaped["pending_failed"],
                tickets_expired=reaped["tickets_expired"],
            )
        if reaped["pending_failed"]:
            # A reaped pending row just freed its device (P2). Ring the session_sync
            # doorbell so the orphan/liveness sweep runs immediately instead of up to one
            # poll interval later — closing the window where the freed device is
            # re-allocatable while a router-crash orphan may still hold it.
            request_session_sync_wake()
        depth = await db.scalar(
            select(func.count())
            .select_from(GridSessionQueueTicket)
            .where(GridSessionQueueTicket.status == GridQueueStatus.waiting)
        )
        GRID_QUEUE_DEPTH.set(float(depth or 0))
        await db.commit()
