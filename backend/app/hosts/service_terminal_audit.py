from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.hosts.models import HostTerminalSession

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession


async def open_session(
    db: AsyncSession,
    *,
    host_id: UUID,
    opened_by: str | None,
    client_ip: str | None,
    shell: str | None,
) -> UUID:
    row = HostTerminalSession(
        host_id=host_id,
        opened_by=opened_by,
        client_ip=client_ip,
        shell=shell,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row.id


async def close_session(
    db: AsyncSession,
    *,
    session_id: UUID,
    close_reason: str,
    agent_pid: int | None = None,
) -> None:
    row = await db.get(HostTerminalSession, session_id)
    if row is None:
        return
    row.closed_at = datetime.now(UTC)
    row.close_reason = close_reason
    if agent_pid is not None:
        row.agent_pid = agent_pid
    await db.commit()
