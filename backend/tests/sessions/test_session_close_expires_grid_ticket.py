"""Regression (audit M2): the testkit session-close paths must terminalize the
allocation grid ticket.

A ticket sits in ``claimed`` for the whole life of an allocation session. The
router ``/sessions/ended`` path (``close_running_session``) expires it, but when
a testkit close (``mark_session_finished`` POST /finished or
``update_session_status`` PATCH /status) wins the race and stamps ``ended_at``
first, ``close_running_session`` re-checks, finds the row already ended, and
no-ops — so neither path expired the ticket and it lingered in ``claimed`` until
the reaper swept it as ``orphan_claim_reaped`` (routine churn that masks a real
router-crash leak alarm). Both testkit paths must expire the ticket themselves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.devices.models import DeviceOperationalState
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.sessions.models import Session, SessionStatus
from app.sessions.service import SessionCrudService
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.db


async def _running_session_with_claimed_ticket(
    db: AsyncSession, host_id: str, *, session_id: str
) -> tuple[Session, GridSessionQueueTicket]:
    device = await create_device(
        db,
        host_id=host_id,
        name=f"close-expires-ticket-{session_id}",
        operational_state=DeviceOperationalState.busy,
    )
    session = Session(session_id=session_id, device_id=device.id, status=SessionStatus.running)
    db.add(session)
    await db.flush()
    ticket = GridSessionQueueTicket(
        requested_body={"capabilities": {"alwaysMatch": {}, "firstMatch": [{}]}},
        status=GridQueueStatus.claimed,
        session_row_id=session.id,
    )
    db.add(ticket)
    await db.commit()
    return session, ticket


async def test_update_session_status_expires_grid_ticket(db_session: AsyncSession, db_host: Host) -> None:
    session, ticket = await _running_session_with_claimed_ticket(
        db_session, db_host.id, session_id="status-expires-ticket-1"
    )
    crud = SessionCrudService(publisher=event_bus, lifecycle=AsyncMock())

    result = await crud.update_session_status(db_session, session.session_id, SessionStatus.passed)

    assert result is not None and result.ended_at is not None
    await db_session.refresh(ticket)
    assert ticket.status == GridQueueStatus.expired
