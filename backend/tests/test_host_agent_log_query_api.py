from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from app.hosts.schemas import AgentLogBatchIngest, ShippedLogLineIngest
from app.hosts.service_agent_logs import write_batch

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.db


@pytest.mark.asyncio
async def test_get_agent_logs_returns_page(client: AsyncClient, db_session: AsyncSession, db_host: Host) -> None:
    now = datetime.now(UTC)
    await write_batch(
        db_session,
        host_id=db_host.id,
        batch=AgentLogBatchIngest(
            boot_id=uuid4(),
            lines=[
                ShippedLogLineIngest(
                    ts=now - timedelta(seconds=i),
                    level="INFO",
                    logger_name="agent.test",
                    message=f"line {i}",
                    sequence_no=i,
                )
                for i in range(3)
            ],
        ),
    )
    resp = await client.get(f"/api/hosts/{db_host.id}/agent-logs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert len(body["lines"]) == 3


@pytest.mark.asyncio
async def test_get_agent_logs_level_warn_expands(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    now = datetime.now(UTC)
    await write_batch(
        db_session,
        host_id=db_host.id,
        batch=AgentLogBatchIngest(
            boot_id=uuid4(),
            lines=[
                ShippedLogLineIngest(ts=now, level="INFO", logger_name="x", message="i", sequence_no=0),
                ShippedLogLineIngest(ts=now, level="WARNING", logger_name="x", message="w", sequence_no=1),
                ShippedLogLineIngest(ts=now, level="ERROR", logger_name="x", message="e", sequence_no=2),
            ],
        ),
    )
    resp = await client.get(f"/api/hosts/{db_host.id}/agent-logs", params={"level": "WARN"})
    assert resp.status_code == 200
    body = resp.json()
    assert {line["level"] for line in body["lines"]} == {"WARNING", "ERROR"}


@pytest.mark.asyncio
async def test_get_agent_logs_empty(client: AsyncClient, db_host: Host) -> None:
    resp = await client.get(f"/api/hosts/{db_host.id}/agent-logs")
    assert resp.status_code == 200
    assert resp.json() == {"lines": [], "total": 0, "has_more": False}
