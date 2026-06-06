"""Leader-owned loop that expires stale allocations and queue tickets (spec §7)."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.core.leader.advisory import LeadershipLost
from app.core.observability import get_logger, observe_background_loop
from app.grid.allocation import GRID_QUEUE_DEPTH
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.sessions.service_sync import request_session_sync_wake

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.grid.services_container import GridServices

logger = get_logger(__name__)
LOOP_NAME = "grid_allocation_reaper"
INTERVAL_SEC = 5.0


class GridAllocationReaperLoop:
    def __init__(self, *, services: GridServices) -> None:
        self._services = services

    async def run(self) -> None:
        """Background loop that fails expired pending sessions and expires stale tickets."""
        while True:
            try:
                async with (
                    observe_background_loop(LOOP_NAME, INTERVAL_SEC).cycle(),
                    self._services.session_factory() as db,
                ):
                    await self.run_cycle(db)
            except LeadershipLost as exc:
                logger.error(
                    "grid_allocation_reaper_loop_leadership_lost",
                    reason=str(exc),
                    action="exiting_process_to_prevent_split_brain",
                )
                os._exit(70)
            except Exception:
                logger.exception("Grid allocation reaper cycle failed")
            await asyncio.sleep(INTERVAL_SEC)

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
