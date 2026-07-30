from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

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


def _make_loop(calls: list[str], *, sync_error: Exception | None = None) -> AppiumSweepLoop:
    async def sync(_db: object) -> None:
        calls.append("sync")
        if sync_error is not None:
            raise sync_error

    async def check_due_devices(*, deadline: float | None = None) -> None:
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
    spend from the same allowance as the probe series."""
    calls: list[str] = []
    loop = _make_loop(calls)

    before = time.monotonic()
    await loop._run_cycle(Mock())
    after = time.monotonic()

    check_mock = loop._services.viability.check_due_devices
    deadline = check_mock.await_args.kwargs["deadline"]
    assert before + SCHEDULED_PASS_BUDGET_SEC <= deadline <= after + SCHEDULED_PASS_BUDGET_SEC
