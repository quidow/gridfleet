"""Query host-scoped events from the persisted system_event table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import desc, func, select

from app.events.models import SystemEvent
from app.hosts.schemas import HostEventEntry, HostEventsPage

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession


async def query_host_events(
    db: AsyncSession,
    *,
    host_id: UUID,
    types: list[str] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HostEventsPage:
    # `data @> '{"host_id": "..."}'` is GIN-indexed; `data->>'host_id' = ...` is not.
    base = select(SystemEvent).where(SystemEvent.data.contains({"host_id": str(host_id)}))
    if types:
        base = base.where(SystemEvent.type.in_(types))
    if since is not None:
        base = base.where(SystemEvent.created_at >= since)
    if until is not None:
        base = base.where(SystemEvent.created_at < until)

    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one())

    rows_stmt = base.order_by(desc(SystemEvent.id)).offset(offset).limit(limit)
    rows = (await db.execute(rows_stmt)).scalars().all()

    events = [
        HostEventEntry(
            event_id=row.event_id,
            type=row.type,
            ts=row.created_at,
            data=row.data,
        )
        for row in rows
    ]
    return HostEventsPage(events=events, total=total, has_more=(offset + len(events)) < total)
