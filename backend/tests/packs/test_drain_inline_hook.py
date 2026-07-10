"""WS-4.1: drain completes inline on the release commit; the janitor stage is backstop only."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.packs.models import DriverPack, PackState
from app.packs.services.lifecycle import complete_drain_if_draining
from app.sessions.models import Session, SessionStatus
from app.sessions.service import close_running_session
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.asyncio, pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]

PACK_ID = "appium-uiautomator2"


async def _draining_pack(db: AsyncSession) -> DriverPack:
    pack = await db.get(DriverPack, PACK_ID)
    assert pack is not None
    pack.state = PackState.draining
    await db.commit()
    return pack


async def test_helper_noop_when_pack_not_draining(db_session: AsyncSession) -> None:
    await complete_drain_if_draining(db_session, PACK_ID)  # enabled pack: no-op, no error
    pack = await db_session.get(DriverPack, PACK_ID)
    assert pack is not None and pack.state == PackState.enabled
    await complete_drain_if_draining(db_session, None)  # None pack_id: no-op


async def test_helper_disables_empty_draining_pack(db_session: AsyncSession) -> None:
    await _draining_pack(db_session)
    await complete_drain_if_draining(db_session, PACK_ID)
    await db_session.commit()
    pack = await db_session.get(DriverPack, PACK_ID)
    assert pack is not None and pack.state == PackState.disabled


async def test_helper_keeps_draining_pack_with_live_session(db_session: AsyncSession, default_host_id: str) -> None:
    device = await create_device_record(
        db_session, host_id=default_host_id, identity_value="drain-hook-busy", name="drain-hook-busy"
    )
    db_session.add(Session(session_id="drain-hook-live", device_id=device.id, status=SessionStatus.running))
    await _draining_pack(db_session)
    await complete_drain_if_draining(db_session, PACK_ID)
    await db_session.commit()
    pack = await db_session.get(DriverPack, PACK_ID)
    assert pack is not None and pack.state == PackState.draining


async def test_closing_last_session_disables_draining_pack(db_session: AsyncSession, default_host_id: str) -> None:
    """Acceptance (spec WS-4.1): drain a pack, end its last session, disabled
    without waiting for the 60s backstop stage."""
    device = await create_device_record(
        db_session, host_id=default_host_id, identity_value="drain-hook-last", name="drain-hook-last"
    )
    db_session.add(Session(session_id="drain-hook-last-session", device_id=device.id, status=SessionStatus.running))
    await _draining_pack(db_session)

    # close_running_session requires session.device loaded (async lazy-load would raise).
    session = (
        await db_session.execute(
            select(Session)
            .options(selectinload(Session.device), selectinload(Session.run))
            .where(Session.session_id == "drain-hook-last-session")
        )
    ).scalar_one()
    await close_running_session(db_session, session, attached_run=None, publisher=AsyncMock())
    await db_session.commit()

    pack = await db_session.get(DriverPack, PACK_ID)
    assert pack is not None and pack.state == PackState.disabled
