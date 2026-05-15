from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from app.hosts.schemas import AgentLogBatchIngest, ShippedLogLineIngest
from app.hosts.service_agent_logs import write_batch

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.db


@pytest.mark.asyncio
async def test_write_batch_inserts_lines(db_session: AsyncSession, db_host: Host) -> None:
    boot_id = uuid4()
    batch = AgentLogBatchIngest(
        boot_id=boot_id,
        lines=[
            ShippedLogLineIngest(
                ts=datetime.now(UTC),
                level="INFO",
                logger_name="agent.lifespan",
                message="started",
                sequence_no=i,
            )
            for i in range(5)
        ],
    )
    result = await write_batch(db_session, host_id=db_host.id, batch=batch)
    assert result.accepted == 5
    assert result.deduped == 0


@pytest.mark.asyncio
async def test_write_batch_deduplicates_on_conflict(db_session: AsyncSession, db_host: Host) -> None:
    boot_id = uuid4()
    lines = [
        ShippedLogLineIngest(
            ts=datetime.now(UTC),
            level="INFO",
            logger_name="agent.x",
            message="m",
            sequence_no=i,
        )
        for i in range(3)
    ]
    first = await write_batch(db_session, host_id=db_host.id, batch=AgentLogBatchIngest(boot_id=boot_id, lines=lines))
    second = await write_batch(db_session, host_id=db_host.id, batch=AgentLogBatchIngest(boot_id=boot_id, lines=lines))
    assert first.accepted == 3
    assert second.accepted == 0
    assert second.deduped == 3


@pytest.mark.asyncio
async def test_write_batch_empty_is_noop(db_session: AsyncSession, db_host: Host) -> None:
    result = await write_batch(
        db_session,
        host_id=db_host.id,
        batch=AgentLogBatchIngest(boot_id=uuid4(), lines=[]),
    )
    assert result.accepted == 0
    assert result.deduped == 0
