from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.background_loop import BackgroundLoop
from app.core.observability import get_logger
from app.packs.models import DriverPack, PackState

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SessionFactory
    from app.packs.services_container import PackServices

logger = get_logger(__name__)
LOOP_NAME = "pack_drain"
POLL_INTERVAL_SEC = 60.0


class PackDrainLoop(BackgroundLoop):
    loop_name = LOOP_NAME
    cycle_failed_message = "Pack drain cycle failed"

    def __init__(self, *, services: PackServices) -> None:
        self._services = services

    @property
    def _session_factory(self) -> SessionFactory:
        return self._services.session_factory

    def _interval(self) -> float:
        return float(POLL_INTERVAL_SEC)

    async def _run_cycle(self, db: AsyncSession) -> None:
        completed = await self._complete_draining_packs_once(db)
        if completed:
            logger.info("Completed draining driver packs: %s", ", ".join(completed))

    async def _complete_draining_packs_once(self, db: AsyncSession) -> list[str]:
        pack_ids = (
            (
                await db.execute(
                    select(DriverPack.id).where(DriverPack.state == PackState.draining).order_by(DriverPack.id)
                )
            )
            .scalars()
            .all()
        )
        completed: list[str] = []
        for pack_id in pack_ids:
            pack = await self._services.lifecycle.try_complete_drain(db, pack_id)
            if pack.state == PackState.disabled:
                completed.append(pack_id)
        await db.commit()
        return completed
