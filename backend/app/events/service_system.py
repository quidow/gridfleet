from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.events.models import SystemEvent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession


async def iter_system_events(
    db: AsyncSession,
    *,
    batch_size: int = 500,
) -> AsyncIterator[SystemEvent]:
    stmt = select(SystemEvent).order_by(SystemEvent.id).execution_options(stream_results=True, yield_per=batch_size)
    async_result = await db.stream_scalars(stmt)
    async for event in async_result:
        yield event
