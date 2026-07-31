from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from app.sessions import appium_sweep
from app.sessions.appium_sweep import AppiumSweepLoop
from app.sessions.service_viability import SCHEDULED_PASS_BUDGET_SEC
from app.sessions.services_container import SessionServices
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import pytest


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def _make_loop(
    calls: list[str],
    *,
    sync_error: Exception | None = None,
    sync_delay_sec: float = 0.0,
    sync_observed_at: list[float] | None = None,
) -> AppiumSweepLoop:
    async def sync(_db: object) -> None:
        if sync_delay_sec:
            await asyncio.sleep(sync_delay_sec)
        if sync_observed_at is not None:
            sync_observed_at.append(time.monotonic())
        calls.append("sync")
        if sync_error is not None:
            raise sync_error

    async def check_due_devices(*, deadline: float) -> None:
        calls.append("viability")

    services = SessionServices(
        crud=Mock(),
        kill=Mock(),
        sync=Mock(sync=AsyncMock(side_effect=sync), wait_for_wake=AsyncMock()),
        viability=Mock(check_due_devices=AsyncMock(side_effect=check_due_devices)),
        settings=FakeSettingsReader({}),
        session_factory=_Session,
        publisher=event_bus,
    )
    return AppiumSweepLoop(services=services)


async def test_cycle_runs_sync_then_throttled_viability() -> None:
    calls: list[str] = []
    loop = _make_loop(calls)

    await loop._run_cycle(Mock())
    assert calls == ["sync", "viability"]

    await loop._run_cycle(Mock())
    assert calls == ["sync", "viability", "sync"]


async def test_viability_pass_runs_again_after_throttle_window(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    loop = _make_loop(calls)

    await loop._run_cycle(Mock())
    assert loop._last_viability_pass is not None
    monkeypatch.setattr(loop, "_last_viability_pass", loop._last_viability_pass - 61.0)
    await loop._run_cycle(Mock())

    assert calls.count("viability") == 2


async def test_sync_failure_does_not_skip_viability() -> None:
    calls: list[str] = []
    loop = _make_loop(calls, sync_error=RuntimeError("boom"))

    await loop._run_cycle(Mock())

    assert calls == ["sync", "viability"]


async def test_cycle_anchors_the_viability_deadline_at_tick_start() -> None:
    """The stall watchdog measures the whole cycle, so the budget must be
    anchored where the cycle starts — the observation sweep and due-set query
    spend from the same allowance as the probe series.

    ``sync`` is given a real, measurable delay before it records its own
    observed time. A tick-start anchor is computed before that delay runs, so
    the forwarded deadline must land at or before ``sync``'s observed time
    plus the budget. An anchor computed after ``sync`` (e.g. moved to just
    before the ``check_due_devices`` call) would let the sweep's own delay
    leak in for free instead of being charged against the budget, pushing the
    deadline past that bound — that is what this test actually pins.
    """
    calls: list[str] = []
    sync_observed_at: list[float] = []
    loop = _make_loop(calls, sync_delay_sec=0.1, sync_observed_at=sync_observed_at)

    before = time.monotonic()
    await loop._run_cycle(Mock())
    after = time.monotonic()

    check_mock = loop._services.viability.check_due_devices
    deadline = check_mock.await_args.kwargs["deadline"]
    assert before + SCHEDULED_PASS_BUDGET_SEC <= deadline <= after + SCHEDULED_PASS_BUDGET_SEC
    assert deadline <= sync_observed_at[0] + SCHEDULED_PASS_BUDGET_SEC


async def test_a_slow_sweep_starves_the_viability_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sync slower than the whole pass budget leaves the viability pass with
    a deadline already in the past, so ``check_due_devices`` admits no series
    at all — while the throttle stamp still records that a pass "ran".

    This is the load-bearing consequence of anchoring the budget at tick start
    (the ``SCHEDULED_PASS_BUDGET_SEC`` comment block documents it), and it was
    derived analytically during review rather than exercised. The failure it
    guards against is silent: nothing errors, nothing logs, the throttle keeps
    reporting a pass per minute, and no device is ever probed.

    NOTE: patch ``appium_sweep.SCHEDULED_PASS_BUDGET_SEC``, not the
    ``service_viability`` original. ``appium_sweep.py`` does ``from ... import
    SCHEDULED_PASS_BUDGET_SEC``, which binds by value at import time; patching
    the source module leaves the loop reading the real 180 s budget, and this
    test would pass while pinning nothing.
    """
    calls: list[str] = []
    sync_observed_at: list[float] = []
    monkeypatch.setattr(appium_sweep, "SCHEDULED_PASS_BUDGET_SEC", 0.05)
    loop = _make_loop(calls, sync_delay_sec=0.1, sync_observed_at=sync_observed_at)

    await loop._run_cycle(Mock())
    after = time.monotonic()

    # The pass was entered and the throttle stamped — the starvation is not a
    # skipped pass, it is a pass with no allowance left.
    assert calls == ["sync", "viability"]
    assert loop._last_viability_pass is not None

    check_mock = loop._services.viability.check_due_devices
    deadline = check_mock.await_args.kwargs["deadline"]
    # ``sync`` slept 0.1 s against a 0.05 s budget, so the line was already
    # crossed before sync even returned. Deterministic: asyncio.sleep(0.1)
    # guarantees at least 0.1 s elapsed, and the anchor is 0.05 s after start.
    assert deadline < sync_observed_at[0]
    assert deadline < after
