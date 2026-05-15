"""Service layer for agent-shipped log ingest and operator queries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.hosts.models import HostAgentLogEntry
from app.hosts.schemas import AgentLogBatchIngest, AgentLogIngestResult, AgentLogLine, AgentLogPage

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession


async def write_batch(
    db: AsyncSession,
    *,
    host_id: UUID,
    batch: AgentLogBatchIngest,
) -> AgentLogIngestResult:
    if not batch.lines:
        return AgentLogIngestResult(accepted=0, deduped=0)

    rows = [
        {
            "host_id": host_id,
            "boot_id": batch.boot_id,
            "sequence_no": line.sequence_no,
            "ts": line.ts,
            "level": line.level,
            "logger_name": line.logger_name,
            "message": line.message,
        }
        for line in batch.lines
    ]

    stmt = (
        pg_insert(HostAgentLogEntry)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_agent_log_seq")
        .returning(HostAgentLogEntry.id)
    )
    result = await db.execute(stmt)
    inserted = len(result.scalars().all())
    await db.commit()

    return AgentLogIngestResult(accepted=inserted, deduped=len(rows) - inserted)


async def query_logs(
    db: AsyncSession,
    *,
    host_id: UUID,
    levels: list[str] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> AgentLogPage:
    base = select(HostAgentLogEntry).where(HostAgentLogEntry.host_id == host_id)
    if levels:
        base = base.where(HostAgentLogEntry.level.in_(levels))
    if since is not None:
        base = base.where(HostAgentLogEntry.ts >= since)
    if until is not None:
        base = base.where(HostAgentLogEntry.ts < until)
    if q:
        # Treat operator input as a literal substring: escape ILIKE wildcards.
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        base = base.where(HostAgentLogEntry.message.ilike(f"%{escaped}%", escape="\\"))

    count_stmt = select(func.count()).select_from(base.subquery())
    total = int((await db.execute(count_stmt)).scalar_one())

    rows_stmt = (
        base.order_by(desc(HostAgentLogEntry.ts), desc(HostAgentLogEntry.sequence_no)).offset(offset).limit(limit)
    )
    rows = (await db.execute(rows_stmt)).scalars().all()

    lines = [
        AgentLogLine(
            ts=row.ts,
            level=row.level,
            logger_name=row.logger_name,
            message=row.message,
            sequence_no=row.sequence_no,
            boot_id=row.boot_id,
        )
        for row in rows
    ]
    return AgentLogPage(lines=lines, total=total, has_more=(offset + len(lines)) < total)
