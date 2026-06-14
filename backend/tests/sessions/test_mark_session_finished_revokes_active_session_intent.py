"""Regression: ``mark_session_finished`` (testkit POST /finished flow) must
revoke the per-session ``active_session:{sid}`` intent. Previously this path
relied on the follow-up PATCH /status call from ``update_session_status`` to do
the cleanup; testkit clients that posted /finished without a follow-up status
leaked one intent per session served.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.core.timeutil import now_utc
from app.devices.models import DeviceIntent, DeviceOperationalState
from app.devices.services.intent_types import NODE_PROCESS
from app.sessions.models import Session, SessionStatus
from app.sessions.service import SessionCrudService
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

    crud = SessionCrudService(publisher=event_bus, lifecycle=AsyncMock())
    result = await crud.mark_session_finished(db_session, session.session_id)

    assert result is not None
    assert result.ended_at is not None
    assert not await _intent_exists(db_session, device.id, source), (
        "mark_session_finished must revoke the active_session intent"
    )


async def test_mark_session_finished_lost_update_race_skips_side_effects(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave-5 re-review B3: the read-then-write close has no row lock, so a
    concurrent closer (e.g. the session_sync sweep on another worker) that
    commits ``ended_at`` between our SELECT and our write went unnoticed — both
    closers stamped the row and double-ran the revoke + lifecycle side effects.
    The conditional UPDATE (``WHERE ended_at IS NULL``) makes the documented
    idempotency true at the DB level: the loser must not re-fire
    ``handle_session_finished`` nor overwrite the winner's stamp.

    The interleaving is forced deterministically: ``get_session`` is wrapped so
    the "other worker's" commit lands right after our read, exactly inside the
    race window."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="mark-finished-race-test",
        operational_state=DeviceOperationalState.busy,
    )
    session = Session(
        session_id="race-sess-1",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()
    winner_stamp = now_utc() - timedelta(seconds=30)

    real_get_session = SessionCrudService.get_session

    async def racing_get_session(self: SessionCrudService, db: AsyncSession, session_id: str) -> Session | None:
        loaded = await real_get_session(self, db, session_id)
        # The other closer terminalizes and commits between our read and write.
        async with db_session_maker() as other_db:
            await other_db.execute(
                update(Session).where(Session.session_id == "race-sess-1").values(ended_at=winner_stamp)
            )
            await other_db.commit()
        return loaded

    monkeypatch.setattr(SessionCrudService, "get_session", racing_get_session)
    lifecycle = AsyncMock()
    crud = SessionCrudService(publisher=event_bus, lifecycle=lifecycle)
    result = await crud.mark_session_finished(db_session, "race-sess-1")

    assert result is not None
    lifecycle.handle_session_finished.assert_not_awaited()
    await db_session.rollback()
    await db_session.refresh(session)
    assert session.ended_at == winner_stamp, "the loser must not overwrite the winner's ended_at"
