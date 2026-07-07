from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from app.sessions.live_session_predicate import device_has_live_session
from app.sessions.models import Session, SessionStatus
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


@pytest.mark.asyncio
async def test_pending_session_counts_as_live(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="live-pending")
    db_session.add(
        Session(
            session_id="alloc-live-pending",
            device_id=device.id,
            status=SessionStatus.pending,
        )
    )
    await db_session.flush()

    assert await device_has_live_session(db_session, device.id) is True


@pytest.mark.asyncio
async def test_ended_session_is_not_live(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="live-ended")
    db_session.add(
        Session(
            session_id="ended-session",
            device_id=device.id,
            status=SessionStatus.passed,
            ended_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    assert await device_has_live_session(db_session, device.id) is False
