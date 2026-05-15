from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import insert, select

from app.devices.services.data_cleanup import _cleanup_old_data
from app.hosts.models import HostAgentLogEntry
from app.settings import settings_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.db


@pytest.mark.asyncio
async def test_prune_deletes_only_old_rows(db_session: AsyncSession, db_host: Host) -> None:
    settings_service._cache["retention.agent_log_days"] = 7
    now = datetime.now(UTC)
    boot_id = uuid4()
    # Cleanup uses server-clock `received_at` (agent `ts` may be skewed).
    await db_session.execute(
        insert(HostAgentLogEntry),
        [
            {
                "host_id": db_host.id,
                "boot_id": boot_id,
                "sequence_no": 0,
                "ts": now,
                "received_at": now - timedelta(days=10),
                "level": "INFO",
                "logger_name": "x",
                "message": "old",
            },
            {
                "host_id": db_host.id,
                "boot_id": boot_id,
                "sequence_no": 1,
                "ts": now,
                "received_at": now - timedelta(days=1),
                "level": "INFO",
                "logger_name": "x",
                "message": "new",
            },
        ],
    )
    await db_session.commit()

    await _cleanup_old_data(db_session)

    rows = (
        (await db_session.execute(select(HostAgentLogEntry).where(HostAgentLogEntry.host_id == db_host.id)))
        .scalars()
        .all()
    )
    assert {row.message for row in rows} == {"new"}
