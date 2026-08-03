"""is_available_ignoring_live_session_sql differs from is_available_sql on exactly one axis."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.core.timeutil import now_utc
from app.devices.models import Device
from app.devices.services.state import is_available_ignoring_live_session_sql, is_available_sql
from app.sessions.models import Session, SessionStatus
from tests.helpers import seed_host_and_running_node
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


@pytest.mark.db
async def test_relaxed_predicate_admits_only_the_live_session_axis(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    _, busy, _ = await seed_host_and_running_node(db_session, identity=f"relaxed-busy-{uuid.uuid4().hex[:8]}")
    _, free, _ = await seed_host_and_running_node(db_session, identity=f"relaxed-free-{uuid.uuid4().hex[:8]}")
    db_session.add(Session(session_id=f"relaxed-{uuid.uuid4().hex}", device_id=busy.id, status=SessionStatus.running))
    await db_session.commit()
    now = now_utc()

    strict = set((await db_session.execute(select(Device.id).where(is_available_sql(now=now)))).scalars())
    relaxed = set(
        (await db_session.execute(select(Device.id).where(is_available_ignoring_live_session_sql(now=now)))).scalars()
    )

    assert free.id in strict
    assert busy.id not in strict
    assert {free.id, busy.id} <= relaxed


@pytest.mark.db
async def test_relaxed_predicate_still_excludes_unverified_devices(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"relaxed-unver-{uuid.uuid4().hex[:8]}")
    device.verified_at = None
    await db_session.commit()
    now = now_utc()

    relaxed = set(
        (await db_session.execute(select(Device.id).where(is_available_ignoring_live_session_sql(now=now)))).scalars()
    )

    assert device.id not in relaxed
