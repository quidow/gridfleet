from __future__ import annotations

from app.core.metrics_recorders import BACKGROUND_LOOP_OVERRUN_TOTAL, record_background_loop_overrun


def test_cycle_within_interval_does_not_overrun() -> None:
    before = BACKGROUND_LOOP_OVERRUN_TOTAL.labels(loop_name="t")._value.get()  # type: ignore[attr-defined]
    record_background_loop_overrun("t", 2.0, interval_seconds=15.0)
    after = BACKGROUND_LOOP_OVERRUN_TOTAL.labels(loop_name="t")._value.get()  # type: ignore[attr-defined]
    assert before == after


def test_cycle_over_interval_increments_overrun() -> None:
    before = BACKGROUND_LOOP_OVERRUN_TOTAL.labels(loop_name="t")._value.get()  # type: ignore[attr-defined]
    record_background_loop_overrun("t", 20.0, interval_seconds=15.0)
    after = BACKGROUND_LOOP_OVERRUN_TOTAL.labels(loop_name="t")._value.get()  # type: ignore[attr-defined]
    assert after == before + 1


def test_zero_interval_never_overruns() -> None:
    before = BACKGROUND_LOOP_OVERRUN_TOTAL.labels(loop_name="t")._value.get()  # type: ignore[attr-defined]
    record_background_loop_overrun("t", 20.0, interval_seconds=0.0)
    after = BACKGROUND_LOOP_OVERRUN_TOTAL.labels(loop_name="t")._value.get()  # type: ignore[attr-defined]
    assert after == before
