from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.core.janitor import JANITOR_BASE_INTERVAL_SEC, JanitorLoop, JanitorStage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


def _loop(stages: tuple[JanitorStage, ...]) -> JanitorLoop:
    return JanitorLoop(session_factory=AsyncMock(), stages=stages)


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


async def test_failing_stage_is_isolated_and_rolls_back() -> None:
    boom = AsyncMock(side_effect=RuntimeError("boom"))
    after = AsyncMock()
    db = AsyncMock()
    loop = _loop(
        (
            JanitorStage("boom", JANITOR_BASE_INTERVAL_SEC, boom),
            JanitorStage("after", JANITOR_BASE_INTERVAL_SEC, after),
        )
    )
    await _run_cycles(loop, db, 1)
    after.assert_awaited_once()
    db.rollback.assert_awaited_once()
