from __future__ import annotations

import time
from unittest.mock import Mock

from app.appium_nodes.services.heartbeat import HeartbeatService
from tests.fakes import FakeSettingsReader


def _svc() -> HeartbeatService:
    return HeartbeatService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=Mock(),
    )


def test_begin_cycle_first_cycle_is_always_guarded() -> None:
    """The first cycle after process start is guarded: last_heartbeat may be
    stale from before a restart while agents are still (re)pushing."""
    svc = _svc()
    guard = svc.begin_cycle()
    assert guard.active is True
    assert guard.gap_sec is None
    assert guard.threshold_sec == 45  # general.host_offline_after_sec default


def test_begin_cycle_quick_follow_up_is_unguarded() -> None:
    svc = _svc()
    svc.begin_cycle()  # first cycle -> guarded
    guard = svc.begin_cycle()  # tiny real monotonic gap
    assert guard.active is False


def test_begin_cycle_reguards_after_a_long_backend_pause() -> None:
    """A monotonic gap wider than host_offline_after_sec means the backend itself
    was paused (preemption, debugger) — re-arm the guard so healthy hosts are not
    flapped offline on the resume cycle."""
    svc = _svc()
    guard = svc.begin_cycle()
    svc._last_cycle_monotonic = time.monotonic() - 10 * guard.threshold_sec
    resumed = svc.begin_cycle()
    assert resumed.active is True
    assert resumed.gap_sec is not None and resumed.gap_sec > guard.threshold_sec
