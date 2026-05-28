"""Regression: ``mark_session_finished`` (testkit POST /finished flow) must
revoke the per-session ``active_session:{sid}`` intent. Previously this path
relied on the follow-up PATCH /status call from ``update_session_status`` to do
the cleanup; testkit clients that posted /finished without a follow-up status
leaked one intent per session served.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.devices.models import DeviceIntent, DeviceOperationalState
from app.devices.services.intent_types import NODE_PROCESS
from app.sessions.models import Session, SessionStatus
from app.sessions.service import SessionCrudService
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.db


async def _intent_exists(db: AsyncSession, device_id: object, source: str) -> bool:
    row = (
        await db.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device_id,
                DeviceIntent.source == source,
            )
        )
    ).scalar_one_or_none()
    return row is not None


async def test_mark_session_finished_revokes_active_session_intent(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """mark_session_finished must revoke active_session:{sid} intent.

    Regression guard for the testkit POST /finished path that previously
    skipped the revoke, leaking one intent per session when the client did
    not follow up with a PATCH /status call.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="mark-finished-revoke-test",
        operational_state=DeviceOperationalState.busy,
    )

    session = Session(
        session_id="test-revoke-sess-1",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(session)

    source = f"active_session:{session.session_id}"
    db_session.add(
        DeviceIntent(
            device_id=device.id,
            source=source,
            axis=NODE_PROCESS,
            payload={"action": "start"},
        )
    )
    await db_session.commit()

    assert await _intent_exists(db_session, device.id, source), "Precondition: intent must exist before the call"

    crud = SessionCrudService(publisher=event_bus)
    result = await crud.mark_session_finished(db_session, session.session_id)

    assert result is not None
    assert result.ended_at is not None
    assert not await _intent_exists(db_session, device.id, source), (
        "mark_session_finished must revoke the active_session intent"
    )
