from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.database import async_session
from app.models.driver_pack import DriverPack, PackState
from app.observability import get_logger, observe_background_loop
from app.services.pack_lifecycle_service import try_complete_drain

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)
LOOP_NAME = "pack_drain"
POLL_INTERVAL_SEC = 60.0


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


async def pack_drain_loop() -> None:
    while True:
        async with observe_background_loop(LOOP_NAME, POLL_INTERVAL_SEC).cycle(), async_session() as db:
            completed = await complete_draining_packs_once(db)
            if completed:
                logger.info("Completed draining driver packs: %s", ", ".join(completed))
        await asyncio.sleep(POLL_INTERVAL_SEC)
