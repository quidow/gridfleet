from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from app.core.observability import _heartbeat_buffer, stalled_background_loop_names
from app.core.timeutil import now_utc

if TYPE_CHECKING:
    from datetime import datetime


def _record_cycle(*, name: str, interval: float, at: datetime) -> None:
    # Drive the real buffer the way BackgroundLoopObservation.cycle does on a
    # successful cycle: started_at == succeeded_at == the cycle time, so
    # next_expected_at lands at ``at + interval``.
    _heartbeat_buffer.update(name, interval_seconds=interval, started_at=at, succeeded_at=at)


def test_fresh_loop_is_not_stalled() -> None:
    _record_cycle(name="wd-fresh", interval=30.0, at=now_utc())
    assert "wd-fresh" not in stalled_background_loop_names(now=now_utc(), extra_grace_seconds=600.0)


def test_long_silent_loop_is_stalled() -> None:
    _record_cycle(name="wd-stale", interval=30.0, at=now_utc() - timedelta(seconds=1800))
    assert "wd-stale" in stalled_background_loop_names(now=now_utc(), extra_grace_seconds=600.0)
