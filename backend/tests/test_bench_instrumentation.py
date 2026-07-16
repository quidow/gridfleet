"""CI-run unit tests for the fold-benchmark instrumentation (tests/bench_instrumentation.py).

These are deliberately NOT gated behind FOLD_BENCH: the taps, profiler, and
report helpers are pure logic and must not rot between benchmark runs.
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter

from tests.bench_instrumentation import (
    ACTIVE_DB_CALLSITE,
    CommitTap,
    QueryTap,
    callsite_label,
    profiled_async_session_method,
)


def test_bench_callsite_label_is_repository_relative() -> None:
    label = callsite_label(sys._getframe())

    assert label == "tests.test_bench_instrumentation.test_bench_callsite_label_is_repository_relative"
    assert "/Users/" not in label
    assert ":" not in label


def test_bench_query_and_commit_taps_group_by_callsite() -> None:
    tap = QueryTap()
    commits = CommitTap()
    token = ACTIVE_DB_CALLSITE.set("app.devices.locking.lock_device")
    try:
        tap(None, None, "SELECT devices.id FROM devices")
        commits(None)
    finally:
        ACTIVE_DB_CALLSITE.reset(token)

    assert tap.callsite_counter == Counter({("app.devices.locking.lock_device", "SELECT devices"): 1})
    assert commits.callsite_counter == Counter({"app.devices.locking.lock_device": 1})


async def test_bench_nested_session_wrapper_preserves_outer_callsite() -> None:
    async def read_active_callsite(_session: object) -> str:
        return ACTIVE_DB_CALLSITE.get()

    inner = profiled_async_session_method(read_active_callsite)

    async def call_inner(session: object) -> str:
        return await inner(session)

    outer = profiled_async_session_method(call_inner)
    observed = await outer(object())

    assert observed == "tests.test_bench_instrumentation.test_bench_nested_session_wrapper_preserves_outer_callsite"


async def test_bench_session_wrapper_relabels_inherited_child_task_context() -> None:
    async def read_active_callsite(_session: object) -> str:
        return ACTIVE_DB_CALLSITE.get()

    wrapped = profiled_async_session_method(read_active_callsite)

    async def run_child() -> str:
        return await wrapped(object())

    token = ACTIVE_DB_CALLSITE.set("app.devices.services.connectivity.fold_host_devices")
    try:
        observed = await asyncio.create_task(run_child())
    finally:
        ACTIVE_DB_CALLSITE.reset(token)

    assert observed == "tests.test_bench_instrumentation.run_child"


def test_bench_cost_partition_separates_deferred_event_work() -> None:
    tap = QueryTap()
    tap.total = 3
    tap.callsite_counter.update(
        {
            ("app.devices.locking.lock_device", "SELECT devices"): 1,
            ("app.events.event_bus._persist_system_event", "INSERT system_events"): 1,
            ("app.events.event_bus._persist_system_event", "SELECT ?"): 1,
        }
    )
    commits = CommitTap()
    commits.count = 3
    commits.callsite_counter.update(
        {
            "app.devices.services.connectivity.fold_host_devices": 1,
            "app.events.event_bus._persist_system_event": 2,
        }
    )

    assert tap.source_total == 1
    assert tap.deferred_total == 2
    assert commits.source_count == 1
    assert commits.deferred_count == 2


def test_bench_percentile_nearest_rank() -> None:
    from tests.bench_instrumentation import percentile

    values = [5.0, 1.0, 3.0, 2.0, 4.0]
    assert percentile(values, 0.5) == 3.0
    assert percentile(values, 0.95) == 5.0
    assert percentile([7.0], 0.95) == 7.0
    assert percentile([], 0.95) == 0.0


class _FakeContext:
    pass


class _FakeCursor:
    rowcount = 3


def test_bench_query_tap_records_duration_rows_and_last_statement() -> None:
    tap = QueryTap()
    context = _FakeContext()
    token = ACTIVE_DB_CALLSITE.set("app.devices.locking.lock_device_handle")
    try:
        tap(None, _FakeCursor(), "SELECT devices.id FROM devices", ("p1",), context, False)
        tap.after(None, _FakeCursor(), "SELECT devices.id FROM devices", ("p1",), context, False)
    finally:
        ACTIVE_DB_CALLSITE.reset(token)

    key = ("app.devices.locking.lock_device_handle", "SELECT devices")
    assert len(tap.durations[key]) == 1
    assert tap.durations[key][0] >= 0.0
    assert tap.rows[key] == 3
    assert tap.last_statement[key] == ("SELECT devices.id FROM devices", ("p1",))


def test_bench_query_tap_after_without_before_is_ignored() -> None:
    tap = QueryTap()
    tap.after(None, _FakeCursor(), "SELECT 1", (), _FakeContext(), False)
    assert tap.durations == {}


def test_bench_query_tap_disarmed_records_nothing() -> None:
    tap = QueryTap()
    tap.armed = False
    context = _FakeContext()
    tap(None, _FakeCursor(), "SELECT devices.id FROM devices", (), context, False)
    tap.after(None, _FakeCursor(), "SELECT devices.id FROM devices", (), context, False)
    assert tap.total == 0
    assert tap.durations == {}


def test_bench_json_report_shape() -> None:
    from tests.bench_instrumentation import build_json_report

    tap = QueryTap()
    key = ("app.devices.locking.lock_device_handle", "SELECT devices")
    tap.total = 4
    tap.counter.update({"SELECT devices": 4})
    tap.callsite_counter.update({key: 4})
    tap.durations[key].extend([1.0, 2.0, 3.0, 4.0])
    tap.rows[key] += 8
    commits = CommitTap()
    commits.count = 2
    commits.callsite_counter.update({"tests.test_bench_folds.test_bench_device_health_loop_fold": 2})

    report = build_json_report(
        config={"scenario": "steady", "devices": 2, "iters": 2},
        tap=tap,
        commits=commits,
        iters=2,
        fold_wall_ms=[10.0, 20.0],
        settled_wall_ms=[11.0, 21.0],
    )

    assert report["config"] == {"scenario": "steady", "devices": 2, "iters": 2}
    assert report["wall_ms"]["fold_return"] == {"median": 10.0, "p95": 20.0, "all": [10.0, 20.0]}
    assert report["queries"] == {"source_per_fold": 2.0, "deferred_per_fold": 0.0, "complete_per_fold": 2.0}
    assert report["commits"] == {"source_per_fold": 1.0, "deferred_per_fold": 0.0, "complete_per_fold": 1.0}
    assert report["signatures"] == {"SELECT devices": 2.0}
    (entry,) = report["callsites"]
    assert entry == {
        "callsite": "app.devices.locking.lock_device_handle",
        "signature": "SELECT devices",
        "calls_per_fold": 2.0,
        "total_ms_per_fold": 5.0,
        "median_ms": 2.0,
        "p95_ms": 4.0,
        "rows_per_fold": 4.0,
    }
    assert report["explain"] == []
