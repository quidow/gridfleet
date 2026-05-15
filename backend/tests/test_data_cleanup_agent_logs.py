from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.devices.services.data_cleanup import _cleanup_old_data
from app.hosts.models import HostAgentLogEntry
from app.hosts.schemas import AgentLogBatchIngest, ShippedLogLineIngest
from app.hosts.service_agent_logs import write_batch
from app.settings import settings_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.db


@pytest.mark.asyncio
async def test_prune_deletes_only_old_rows(db_session: AsyncSession, db_host: Host) -> None:
    settings_service._cache["retention.agent_log_days"] = 7
    old_ts = datetime.now(UTC) - timedelta(days=10)
    new_ts = datetime.now(UTC) - timedelta(days=1)
    await write_batch(
        db_session,
        host_id=db_host.id,
        batch=AgentLogBatchIngest(
            boot_id=uuid4(),
            lines=[
                ShippedLogLineIngest(ts=old_ts, level="INFO", logger_name="x", message="old", sequence_no=0),
                ShippedLogLineIngest(ts=new_ts, level="INFO", logger_name="x", message="new", sequence_no=1),
            ],
        ),
    )

    await _cleanup_old_data(db_session)

    rows = (
        (await db_session.execute(select(HostAgentLogEntry).where(HostAgentLogEntry.host_id == db_host.id)))
        .scalars()
        .all()
    )
    assert {row.message for row in rows} == {"new"}
