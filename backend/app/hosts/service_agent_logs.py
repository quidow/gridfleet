"""Service layer for agent-shipped log ingest and operator queries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.hosts.models import HostAgentLogEntry
from app.hosts.schemas import AgentLogBatchIngest, AgentLogIngestResult

if TYPE_CHECKING:
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
