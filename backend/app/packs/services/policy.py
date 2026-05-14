from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.packs.models import DriverPack, DriverPackRelease

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.packs.schemas import RuntimePolicy


async def set_runtime_policy(session: AsyncSession, pack_id: str, policy: RuntimePolicy) -> DriverPack:
    pack = await session.get(DriverPack, pack_id)
    if pack is None:
        raise LookupError(pack_id)
    pack.runtime_policy = policy.model_dump()
    await session.commit()
    reloaded = (
        await session.execute(
            select(DriverPack)
            .options(
                selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms),
                selectinload(DriverPack.releases).selectinload(DriverPackRelease.features),
            )
            .where(DriverPack.id == pack_id)
        )
    ).scalar_one()
    return reloaded
