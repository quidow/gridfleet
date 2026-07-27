from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.janitor import JANITOR_BASE_INTERVAL_SEC, JanitorLoop, JanitorStage
from tests.fakes import FakeSessionFactory, RecordingSessionFactory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


def _loop(stages: tuple[JanitorStage, ...]) -> JanitorLoop:
    return JanitorLoop(session_factory=FakeSessionFactory(), stages=stages)


async def _run_cycles(loop: JanitorLoop, db: AsyncSession, count: int) -> None:
    for _ in range(count):
        await loop._run_cycle(db)
        loop._on_cycle_end(0.0, JANITOR_BASE_INTERVAL_SEC)


async def test_stages_run_at_their_own_cadence() -> None:
    every_tick = AsyncMock()
    every_four = AsyncMock()
    loop = _loop(
        (
            JanitorStage("every_tick", JANITOR_BASE_INTERVAL_SEC, every_tick),
            JanitorStage("every_four", JANITOR_BASE_INTERVAL_SEC * 4, every_four),
        )
    )
    await _run_cycles(loop, AsyncMock(), 5)
    assert every_tick.await_count == 5
    assert every_four.await_count == 2  # cycles 0 and 4


async def test_skip_first_cycle_stage_skips_boot_cycle() -> None:
    hourly = AsyncMock()
    loop = _loop((JanitorStage("hourly", 3600.0, hourly, skip_first_cycle=True),))
    await _run_cycles(loop, AsyncMock(), 240)
    assert hourly.await_count == 0
    await _run_cycles(loop, AsyncMock(), 1)  # cycle index 240 = one hour of ticks
    assert hourly.await_count == 1


async def test_failing_stage_closes_its_session_before_next_stage(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    factory = RecordingSessionFactory(db_session_maker)
    ran_after = False

    async def boom(db: AsyncSession) -> None:
        await db.execute(select(1))
        raise RuntimeError("boom")

    async def after(db: AsyncSession) -> None:
        nonlocal ran_after
        await db.execute(select(1))
        ran_after = True

    loop = JanitorLoop(
        session_factory=factory,
        stages=(
            JanitorStage("boom", JANITOR_BASE_INTERVAL_SEC, boom),
            JanitorStage("after", JANITOR_BASE_INTERVAL_SEC, after),
        ),
    )

    await loop._run_cycle(AsyncMock())

    assert len(factory.sessions) == 2
    assert factory.sessions[0] is not factory.sessions[1]
    assert all(not db.in_transaction() for db in factory.sessions)
    assert ran_after
