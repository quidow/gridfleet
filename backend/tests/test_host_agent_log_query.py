from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from app.hosts.models import Host, HostStatus, OSType
from app.hosts.schemas import AgentLogBatchIngest, ShippedLogLineIngest
from app.hosts.service_agent_logs import query_logs, write_batch

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.db


async def _create_host(db_session: AsyncSession, hostname: str) -> Host:
    host = Host(
        hostname=hostname,
        ip="10.0.0.42",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()
    return host


async def _seed(
    db_session: AsyncSession,
    host_id: UUID,
    *,
    lines: list[tuple[str, str, datetime, int]],
) -> None:  # type: ignore[no-untyped-def]
    boot = uuid4()
    batch = AgentLogBatchIngest(
        boot_id=boot,
        lines=[
            ShippedLogLineIngest(ts=ts, level=level, logger_name="agent.test", message=message, sequence_no=seq)
            for (level, message, ts, seq) in lines
        ],
    )
    await write_batch(db_session, host_id=host_id, batch=batch)


@pytest.mark.asyncio
async def test_query_levels_filter(db_session: AsyncSession, db_host: Host) -> None:
    now = datetime.now(UTC)
    await _seed(
        db_session,
        db_host.id,
        lines=[
            ("INFO", "i1", now - timedelta(seconds=3), 0),
            ("WARNING", "w1", now - timedelta(seconds=2), 1),
            ("ERROR", "e1", now - timedelta(seconds=1), 2),
        ],
    )
    page = await query_logs(db_session, host_id=db_host.id, levels=["WARNING", "ERROR"], limit=10, offset=0)
    assert page.total == 2
    assert {line.level for line in page.lines} == {"WARNING", "ERROR"}


@pytest.mark.asyncio
async def test_query_time_range(db_session: AsyncSession, db_host: Host) -> None:
    now = datetime.now(UTC)
    await _seed(
        db_session,
        db_host.id,
        lines=[
            ("INFO", "old", now - timedelta(hours=2), 0),
            ("INFO", "new", now - timedelta(minutes=5), 1),
        ],
    )
    page = await query_logs(
        db_session,
        host_id=db_host.id,
        since=now - timedelta(minutes=10),
        limit=10,
        offset=0,
    )
    assert {line.message for line in page.lines} == {"new"}


@pytest.mark.asyncio
async def test_query_substring_search(db_session: AsyncSession, db_host: Host) -> None:
    now = datetime.now(UTC)
    await _seed(
        db_session,
        db_host.id,
        lines=[
            ("INFO", "fooBAR", now, 0),
            ("INFO", "baz", now, 1),
        ],
    )
    page = await query_logs(db_session, host_id=db_host.id, q="bar", limit=10, offset=0)
    assert {line.message for line in page.lines} == {"fooBAR"}


@pytest.mark.asyncio
async def test_query_cross_host_isolation(db_session: AsyncSession, db_host: Host) -> None:
    host_b = await _create_host(db_session, f"query-host-{uuid4().hex[:8]}")
    now = datetime.now(UTC)
    await _seed(db_session, db_host.id, lines=[("INFO", "a", now, 0)])
    await _seed(db_session, host_b.id, lines=[("INFO", "b", now, 0)])
    page = await query_logs(db_session, host_id=db_host.id, limit=10, offset=0)
    assert {line.message for line in page.lines} == {"a"}


@pytest.mark.asyncio
async def test_query_pagination(db_session: AsyncSession, db_host: Host) -> None:
    now = datetime.now(UTC)
    await _seed(
        db_session,
        db_host.id,
        lines=[("INFO", f"m{i}", now + timedelta(milliseconds=i), i) for i in range(5)],
    )
    page_first = await query_logs(db_session, host_id=db_host.id, limit=2, offset=0)
    page_second = await query_logs(db_session, host_id=db_host.id, limit=2, offset=2)
    assert page_first.total == 5
    assert page_first.has_more is True
    assert len(page_first.lines) == 2
    assert len(page_second.lines) == 2
    assert {line.message for line in page_first.lines} & {line.message for line in page_second.lines} == set()
