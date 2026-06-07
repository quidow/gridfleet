"""Unit tests for the shared BackgroundLoop scaffold."""

from __future__ import annotations

import asyncio

import pytest

from app.core.background_loop import BackgroundLoop
from app.core.leader.advisory import LeadershipLost


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def _fake_session_factory() -> _FakeSession:
    return _FakeSession()


class _RecordingLoop(BackgroundLoop):
    loop_name = "scaffold_test"
    exit_on_leadership_lost = True
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

    async def _run_cycle(self, db) -> None:  # noqa: ANN001 - stub session
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


async def test_leadership_lost_exits_process_when_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[int] = []

    class _Exited(BaseException):
        pass

    def _fake_exit(code: int) -> None:
        recorded.append(code)
        raise _Exited

    monkeypatch.setattr("app.core.background_loop.os._exit", _fake_exit)
    loop = _RecordingLoop(fail_with=LeadershipLost("stolen"))
    task = asyncio.create_task(loop.run())
    with pytest.raises(_Exited):
        await task
    assert recorded == [70]
    assert len(loop.ends) == 1  # cycle observability recorded before exit


async def test_leadership_lost_continues_when_not_flagged() -> None:
    class _TolerantLoop(_RecordingLoop):
        exit_on_leadership_lost = False

    loop = _TolerantLoop(fail_with=LeadershipLost("stolen"))
    await _run_cycles(loop, until=lambda: loop.cycles >= 2)
    assert loop.cycles >= 2  # treated as a generic cycle failure
    assert len(loop.ends) >= 2  # _on_cycle_end fires on the leadership-loss path too


async def test_sleep_before_first_cycle() -> None:
    class _SleepFirstLoop(_RecordingLoop):
        sleep_before_first_cycle = True

    loop = _SleepFirstLoop()
    await _run_cycles(loop, until=lambda: loop.cycles >= 1)
    assert loop.events[0] == "wait"  # waited before any cycle


async def test_on_start_runs_once_before_everything() -> None:
    class _StartLoop(_RecordingLoop):
        async def _on_start(self) -> None:
            self.events.append("start")

    loop = _StartLoop()
    await _run_cycles(loop, until=lambda: loop.cycles >= 2)
    assert loop.events[0] == "start"
    assert loop.events.count("start") == 1


def test_default_leadership_event_name() -> None:
    assert _RecordingLoop()._leadership_lost_event() == "scaffold_test_loop_leadership_lost"


async def test_base_default_hooks_are_safe_noops() -> None:
    class _BareLoop(BackgroundLoop):
        loop_name = "scaffold_bare"
        exit_on_leadership_lost = False
        cycle_failed_message = "scaffold bare cycle failed"

        def __init__(self) -> None:
            self.cycles = 0

        @property
        def _session_factory(self) -> object:  # type: ignore[override]
            return _fake_session_factory

        def _interval(self) -> float:
            return 0.0

        async def _run_cycle(self, db) -> None:  # noqa: ANN001 - stub session
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


def test_leadership_event_name_override() -> None:
    class _LegacyNameLoop(_RecordingLoop):
        def _leadership_lost_event(self) -> str:
            return "legacy_leadership_lost"

    assert _LegacyNameLoop()._leadership_lost_event() == "legacy_leadership_lost"
