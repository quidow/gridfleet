from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from app.events import event_bus
from app.hosts.service_host_events import query_host_events

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.db


async def _emit(host_id: UUID, event_type: str, *, extra: dict[str, object] | None = None) -> None:
    payload: dict[str, object] = {"host_id": str(host_id), "hostname": "h"}
    if extra:
        payload.update(extra)
    await event_bus.publish(event_type, payload)
    await event_bus.drain_handlers()


@pytest.mark.asyncio
async def test_query_filters_by_host_id(db_session: AsyncSession, db_host: Host) -> None:
    other_host_id = uuid4()
    await _emit(db_host.id, "host.status_changed", extra={"old_status": "online", "new_status": "degraded"})
    await _emit(other_host_id, "host.status_changed")
    page = await query_host_events(db_session, host_id=db_host.id, limit=10, offset=0)
    assert page.total == 1
    assert page.events[0].data["host_id"] == str(db_host.id)


@pytest.mark.asyncio
async def test_query_filters_by_type(db_session: AsyncSession, db_host: Host) -> None:
    await _emit(db_host.id, "host.status_changed")
    await _emit(db_host.id, "host.heartbeat_lost", extra={"missed_count": 3})
    page = await query_host_events(
        db_session,
        host_id=db_host.id,
        types=["host.heartbeat_lost"],
        limit=10,
        offset=0,
    )
    assert {event.type for event in page.events} == {"host.heartbeat_lost"}


@pytest.mark.asyncio
async def test_query_time_range(db_session: AsyncSession, db_host: Host) -> None:
    await _emit(db_host.id, "host.status_changed")
    cutoff = datetime.now(UTC) + timedelta(seconds=1)
    page = await query_host_events(db_session, host_id=db_host.id, since=cutoff, limit=10, offset=0)
    assert page.total == 0
