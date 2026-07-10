"""Unit tests for the shared BackgroundLoop scaffold."""

from __future__ import annotations

import asyncio
import types
from typing import TYPE_CHECKING

import pytest

from app.core.background_loop import BackgroundLoop

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def _fake_session_factory() -> _FakeSession:
    return _FakeSession()


class _RecordingLoop(BackgroundLoop):
    loop_name = "scaffold_test"
    cycle_failed_message = "scaffold test cycle failed"

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.cycles = 0
        self.ends: list[tuple[float, float]] = []
        self.errors = 0
        self.events: list[str] = []
        self._fail_with = fail_with

    @property
    def _session_factory(self) -> object:  # type: ignore[override]
        return _fake_session_factory

    def _interval(self) -> float:
        return 0.0

    async def _run_cycle(self, db: AsyncSession) -> None:
        self.cycles += 1
        self.events.append("cycle")
        if self._fail_with is not None:
            raise self._fail_with

    async def _wait(self, interval: float) -> None:
        self.events.append("wait")
        await asyncio.sleep(0)

    def _on_cycle_end(self, elapsed_seconds: float, interval: float) -> None:
        self.ends.append((elapsed_seconds, interval))

    def _on_cycle_error(self) -> None:
        self.errors += 1


async def _run_cycles(loop: BackgroundLoop, *, until: object) -> None:
    task = asyncio.create_task(loop.run())
    try:
        for _ in range(200):
            if until():
                return
            await asyncio.sleep(0)
        pytest.fail("loop did not reach expected state")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_runs_cycles_and_waits_between_them() -> None:
    loop = _RecordingLoop()
    await _run_cycles(loop, until=lambda: loop.cycles >= 3)
    assert loop.cycles >= 3
    # work-first ordering: cycle precedes wait
    assert loop.events[:2] == ["cycle", "wait"]
    # _on_cycle_end fired for every completed cycle with the interval
    assert len(loop.ends) >= 3
    assert loop.ends[0][1] == 0.0


async def test_generic_exception_is_contained_and_loop_continues() -> None:
    loop = _RecordingLoop(fail_with=ValueError("boom"))
    await _run_cycles(loop, until=lambda: loop.cycles >= 2)
    assert loop.cycles >= 2  # survived the first failure
    assert loop.errors >= 2  # _on_cycle_error fired per failure
    assert len(loop.ends) >= 2  # _on_cycle_end fired on the failure path too


async def test_on_start_runs_once_before_everything() -> None:
    class _StartLoop(_RecordingLoop):
        async def _on_start(self) -> None:
            self.events.append("start")

    loop = _StartLoop()
    await _run_cycles(loop, until=lambda: loop.cycles >= 2)
    assert loop.events[0] == "start"
    assert loop.events.count("start") == 1


async def test_base_default_hooks_are_safe_noops() -> None:
    class _BareLoop(BackgroundLoop):
        loop_name = "scaffold_bare"
        cycle_failed_message = "scaffold bare cycle failed"

        def __init__(self) -> None:
            self.cycles = 0

        @property
        def _session_factory(self) -> object:  # type: ignore[override]
            return _fake_session_factory

        def _interval(self) -> float:
            return 0.0

        async def _run_cycle(self, db: AsyncSession) -> None:
            self.cycles += 1
            if self.cycles == 1:
                raise ValueError("first cycle fails")

    loop = _BareLoop()
    task = asyncio.create_task(loop.run())
    try:
        for _ in range(200):
            if loop.cycles >= 3:
                break
            await asyncio.sleep(0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert loop.cycles >= 3


class _ClockLoop(_RecordingLoop):
    """Drives a fake monotonic clock so cadence math is deterministic.

    `cycle_cost` is added to the clock during each cycle body; `_wait` records
    the sleep it is asked for and advances the clock by that amount (mimicking a
    real sleep) so the observed period is `cycle_cost + sleep`.
    """

    def __init__(self, *, interval: float, cycle_cost: float) -> None:
        super().__init__()
        self._interval_seconds = interval
        self._cycle_cost = cycle_cost
        self.clock = 0.0
        self.waits: list[float] = []

    def _interval(self) -> float:
        return self._interval_seconds

    async def _run_cycle(self, db: AsyncSession) -> None:
        self.cycles += 1
        self.clock += self._cycle_cost

    async def _wait(self, interval: float) -> None:
        self.waits.append(interval)
        self.clock += interval
        await asyncio.sleep(0)


async def test_wait_uses_interval_minus_cycle_elapsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """run() must sleep `interval - elapsed` so the period is a true cadence."""
    loop = _ClockLoop(interval=10.0, cycle_cost=4.0)
    monkeypatch.setattr("app.core.background_loop.time", types.SimpleNamespace(monotonic=lambda: loop.clock))

    await _run_cycles(loop, until=lambda: len(loop.waits) >= 2)
    # 10s interval minus the 4s the cycle consumed → 6s sleep, every cycle.
    assert loop.waits[0] == pytest.approx(6.0)
    assert loop.waits[1] == pytest.approx(6.0)


async def test_wait_clamps_to_zero_when_cycle_overruns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cycle longer than its interval sleeps 0, never a negative duration."""
    loop = _ClockLoop(interval=10.0, cycle_cost=15.0)
    monkeypatch.setattr("app.core.background_loop.time", types.SimpleNamespace(monotonic=lambda: loop.clock))

    await _run_cycles(loop, until=lambda: len(loop.waits) >= 1)
    assert loop.waits[0] == 0.0


async def test_effective_period_gauge_reflects_true_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    """The effective-period gauge reports the real cadence, not the configured interval."""
    from app.core.metrics_recorders import BACKGROUND_LOOP_EFFECTIVE_PERIOD_SECONDS

    class _IsolatedClockLoop(_ClockLoop):
        loop_name = "scaffold_effective_period"  # isolate the gauge label from other tests

    loop = _IsolatedClockLoop(interval=10.0, cycle_cost=4.0)
    monkeypatch.setattr("app.core.background_loop.time", types.SimpleNamespace(monotonic=lambda: loop.clock))

    # Wait for the second sleep so the first iteration's post-wait gauge write has run.
    await _run_cycles(loop, until=lambda: len(loop.waits) >= 2)
    # 4s cycle + 6s sleep = 10s real period, matching the configured interval.
    gauge = BACKGROUND_LOOP_EFFECTIVE_PERIOD_SECONDS.labels(loop_name="scaffold_effective_period")
    assert gauge._value.get() == pytest.approx(10.0)  # type: ignore[attr-defined]
