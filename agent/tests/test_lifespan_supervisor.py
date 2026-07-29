"""Restart supervision for the lifespan's background loops.

Covers the two properties PR #912 listed as known exposures: a crashing loop
must back off instead of respawning at event-loop speed, and a crash callback
that lands after teardown must not spawn a task teardown has already walked past.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_app.lifespan import (
    _RESTART_BASE_DELAY_SEC,
    _RESTART_HEALTHY_AFTER_SEC,
    _RESTART_MAX_DELAY_SEC,
    _restart_delay,
    _SupervisedTask,
)


def test_restart_delay_schedule() -> None:
    """The first crash of an episode is free; a crash loop pays doubling seconds.

    ``consecutive_crashes`` counts crashes already seen, so 0 is the first crash
    of an episode. Status pushes are what keep a host reading online, so the
    single-crash case — the one PR #912 exists to prevent — costs no silence.
    """
    delays = [_restart_delay(n, _RESTART_BASE_DELAY_SEC, _RESTART_MAX_DELAY_SEC) for n in range(9)]

    assert delays == [0.0, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_restart_delay_is_capped_far_out() -> None:
    """A loop that has been crashing for hours still retries, just no faster than the cap."""
    assert _restart_delay(40, _RESTART_BASE_DELAY_SEC, _RESTART_MAX_DELAY_SEC) == _RESTART_MAX_DELAY_SEC


def test_restart_delay_is_capped_above_float_exponent_limit() -> None:
    """Capping must happen before an unrepresentable exponent is evaluated."""
    assert _restart_delay(1025, _RESTART_BASE_DELAY_SEC, _RESTART_MAX_DELAY_SEC) == _RESTART_MAX_DELAY_SEC


def test_restart_constants_are_the_documented_values() -> None:
    """docs/reference/architecture.md quotes these three numbers; keep them honest."""
    assert (_RESTART_BASE_DELAY_SEC, _RESTART_MAX_DELAY_SEC, _RESTART_HEALTHY_AFTER_SEC) == (1.0, 60.0, 60.0)


class _CrashCounter:
    """Task factory whose first ``crashes`` tasks raise, and whose next one parks.

    Counting inside the coroutine, not inside ``__call__``, is deliberate: it
    makes the count reflect tasks that actually ran, so a test that asserts "no
    restart happened" cannot be fooled by a task created but never scheduled.
    """

    def __init__(self, crashes: int) -> None:
        self._crashes = crashes
        self.starts = 0

    def __call__(self) -> asyncio.Task[None]:
        return asyncio.create_task(self._run())

    async def _run(self) -> None:
        self.starts += 1
        if self.starts <= self._crashes:
            raise RuntimeError(f"synthetic crash {self.starts}")
        await asyncio.Event().wait()


async def _settle(ticks: int = 6) -> None:
    """Drain the loop's ready queue without advancing the clock.

    One ``sleep(0)`` runs one batch of ready callbacks. A crash-and-restart cycle
    needs two batches (the task raises, then its queued done-callback runs), so
    tests that expect N restarts need at least 2N + 1 ticks.
    """
    for _ in range(ticks):
        await asyncio.sleep(0)


async def _wait_for_starts(factory: _CrashCounter, expected: int, timeout: float = 2.0) -> None:
    """Poll rather than sleep a fixed span, so a slow machine does not flake."""
    deadline = asyncio.get_running_loop().time() + timeout
    while factory.starts < expected and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert factory.starts == expected, f"expected {expected} starts, saw {factory.starts}"


@pytest.mark.asyncio
async def test_supervisor_restarts_the_first_crash_immediately() -> None:
    factory = _CrashCounter(crashes=1)
    supervised = _SupervisedTask("t", factory)
    supervised.start()
    try:
        await _settle()
        assert factory.starts == 2
    finally:
        supervised.shutdown()


@pytest.mark.asyncio
async def test_supervisor_defers_the_restart_after_a_second_crash() -> None:
    """The exposure: without backoff this respawns on every event-loop pass."""
    factory = _CrashCounter(crashes=2)
    supervised = _SupervisedTask("t", factory, base_delay=0.05, max_delay=0.05)
    supervised.start()
    try:
        await _settle()
        assert factory.starts == 2, "first crash restarts immediately"
        await _settle()
        assert factory.starts == 2, "second crash must wait out the backoff, not respawn in-batch"
        await _wait_for_starts(factory, 3)
    finally:
        supervised.shutdown()


@pytest.mark.asyncio
async def test_supervisor_resets_the_backoff_after_a_healthy_run() -> None:
    """``healthy_after=0`` makes every task count as healthy, so no crash ever defers.

    With the reset branch missing, the second crash would schedule a 30 s timer
    and this stops at two starts.
    """
    factory = _CrashCounter(crashes=3)
    supervised = _SupervisedTask("t", factory, base_delay=30.0, healthy_after=0.0)
    supervised.start()
    try:
        await _settle(ticks=10)
        assert factory.starts == 4
    finally:
        supervised.shutdown()


@pytest.mark.asyncio
async def test_supervisor_does_not_restart_a_crash_that_lands_with_shutdown() -> None:
    """The exposure: a done-callback is queued through ``call_soon``, not run inline.

    One ``sleep(0)`` is enough for the task to raise and for its callback to be
    queued behind us. ``shutdown()`` then runs in exactly the position the
    lifespan's ``finally`` occupies: after the crash, before the callback. Without
    the gate the callback spawns a replacement nothing will ever cancel.
    """
    factory = _CrashCounter(crashes=1)
    supervised = _SupervisedTask("t", factory)
    task = supervised.start()

    await asyncio.sleep(0)
    assert task.done() and not task.cancelled(), "the task must have crashed but not yet been observed"

    supervised.shutdown()
    await _settle()

    assert factory.starts == 1


@pytest.mark.asyncio
async def test_supervisor_shutdown_cancels_a_pending_backoff_timer() -> None:
    factory = _CrashCounter(crashes=2)
    supervised = _SupervisedTask("t", factory, base_delay=0.05, max_delay=0.05)
    supervised.start()
    await _settle()
    assert factory.starts == 2
    timer = supervised._timer
    assert timer is not None

    supervised.shutdown()
    assert timer.cancelled()
    await asyncio.sleep(0.2)

    assert factory.starts == 2


@pytest.mark.asyncio
async def test_supervisor_shutdown_cancels_the_running_task() -> None:
    factory = _CrashCounter(crashes=0)
    supervised = _SupervisedTask("t", factory)
    task = supervised.start()
    await _settle()

    supervised.shutdown()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)
    assert factory.starts == 1, "a cancelled task must not be restarted"


@pytest.mark.asyncio
async def test_supervisor_does_not_restart_a_clean_exit() -> None:
    """Today's behaviour, kept: ``run_forever`` returning is anomalous, not a crash."""
    starts = 0

    async def _return_immediately() -> None:
        return None

    def _factory() -> asyncio.Task[None]:
        nonlocal starts
        starts += 1
        return asyncio.create_task(_return_immediately())

    supervised = _SupervisedTask("t", _factory)
    supervised.start()
    try:
        await _settle()
        assert starts == 1
    finally:
        supervised.shutdown()
