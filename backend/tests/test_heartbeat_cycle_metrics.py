from __future__ import annotations

from app.metrics_recorders import (
    HEARTBEAT_CYCLE_OVERRUN_TOTAL,
    record_heartbeat_cycle,
)


def test_cycle_within_interval_does_not_overrun() -> None:
    before = HEARTBEAT_CYCLE_OVERRUN_TOTAL._value.get()  # type: ignore[attr-defined]
    record_heartbeat_cycle(2.0, interval_seconds=15.0)
    after = HEARTBEAT_CYCLE_OVERRUN_TOTAL._value.get()  # type: ignore[attr-defined]
    assert before == after


def test_cycle_over_interval_increments_overrun() -> None:
    before = HEARTBEAT_CYCLE_OVERRUN_TOTAL._value.get()  # type: ignore[attr-defined]
    record_heartbeat_cycle(20.0, interval_seconds=15.0)
    after = HEARTBEAT_CYCLE_OVERRUN_TOTAL._value.get()  # type: ignore[attr-defined]
    assert after == before + 1
