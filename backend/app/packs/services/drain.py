from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.observability import get_logger, observe_background_loop
from app.packs.models import DriverPack, PackState
from app.packs.services.lifecycle import try_complete_drain

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.packs.services_container import PackServices

logger = get_logger(__name__)
LOOP_NAME = "pack_drain"
POLL_INTERVAL_SEC = 60.0


class PackDrainLoop:
    def __init__(self, *, services: PackServices) -> None:
        self._services = services

    async def run(self) -> None:
        while True:
            async with (
                observe_background_loop(LOOP_NAME, POLL_INTERVAL_SEC).cycle(),
                self._services.session_factory() as db,
            ):
                completed = await complete_draining_packs_once(db)
                if completed:
                    logger.info("Completed draining driver packs: %s", ", ".join(completed))
            await asyncio.sleep(POLL_INTERVAL_SEC)


async def complete_draining_packs_once(db: AsyncSession) -> list[str]:
    pack_ids = (
        (await db.execute(select(DriverPack.id).where(DriverPack.state == PackState.draining).order_by(DriverPack.id)))
        .scalars()
        .all()
    )
    completed: list[str] = []
    for pack_id in pack_ids:
        pack = await try_complete_drain(db, pack_id)
        if pack.state == PackState.disabled:
            completed.append(pack_id)
    await db.commit()
    return completed
