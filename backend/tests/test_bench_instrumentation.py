"""CI-run unit tests for the fold-benchmark instrumentation (tests/bench_instrumentation.py).

These are deliberately NOT gated behind FOLD_BENCH: the taps, profiler, and
report helpers are pure logic and must not rot between benchmark runs.
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter

import pytest

from tests import bench_instrumentation
from tests.bench_instrumentation import (
    ACTIVE_DB_CALLSITE,
    CommitTap,
    QueryTap,
    callsite_label,
    profiled_async_session_method,
)


@pytest.mark.parametrize(
    ("devices", "iters", "warmup", "churn"),
    [
        (1, 1, 0, 0.0),
        (50, 3, 1, 1.0),
    ],
)
def test_bench_validate_benchmark_knobs_accepts_valid_boundaries(
    devices: int,
    iters: int,
    warmup: int,
    churn: float,
) -> None:
    assert (
        bench_instrumentation.validate_benchmark_knobs(
            devices=devices,
            iters=iters,
            warmup=warmup,
            churn=churn,
        )
        is None
    )


@pytest.mark.parametrize(
    ("devices", "iters", "warmup", "churn", "message"),
    [
        (0, 3, 1, 0.0, "FOLD_BENCH_DEVICES must be > 0, got 0"),
        (50, 0, 1, 0.0, "FOLD_BENCH_ITERS must be > 0, got 0"),
        (50, 3, -1, 0.0, "FOLD_BENCH_WARMUP must be >= 0, got -1"),
        (50, 3, 1, -0.1, "FOLD_BENCH_CHURN must be between 0 and 1, got -0.1"),
        (50, 3, 1, 1.1, "FOLD_BENCH_CHURN must be between 0 and 1, got 1.1"),
        (50, 3, 1, float("nan"), "FOLD_BENCH_CHURN must be between 0 and 1, got nan"),
    ],
)
def test_bench_validate_benchmark_knobs_rejects_invalid_values(
    devices: int,
    iters: int,
    warmup: int,
    churn: float,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        bench_instrumentation.validate_benchmark_knobs(
            devices=devices,
            iters=iters,
            warmup=warmup,
            churn=churn,
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


def test_bench_taps_total_across_every_callsite() -> None:
    """Events now stage inside the source transaction, so there is no second event
    transaction to partition out: every statement and commit is fold cost, and the
    totals aggregate across call sites rather than splitting into source/deferred."""
    tap = QueryTap()
    commits = CommitTap()
    for callsite in (
        "app.devices.locking.lock_device",
        "app.devices.services.connectivity.fold_host_devices",
    ):
        token = ACTIVE_DB_CALLSITE.set(callsite)
        try:
            tap(None, None, "SELECT devices.id FROM devices")
            commits(None)
        finally:
            ACTIVE_DB_CALLSITE.reset(token)

    assert tap.total == 2
    assert commits.count == 2
    assert sum(tap.callsite_counter.values()) == tap.total
    assert sum(commits.callsite_counter.values()) == commits.count


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
    assert tap.captured_parameter_values(key) == {"p1"}


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
        poll_delivery_wall_ms=[11.0, 21.0],
    )

    assert report["config"] == {"scenario": "steady", "devices": 2, "iters": 2}
    assert report["wall_ms"]["fold_return"] == {"median": 10.0, "p95": 20.0, "all": [10.0, 20.0]}
    assert report["queries"] == {"source_per_fold": 2.0, "complete_per_fold": 2.0}
    assert report["commits"] == {"source_per_fold": 1.0, "complete_per_fold": 1.0}
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


def test_bench_explain_never_analyzes_writes() -> None:
    from tests.bench_instrumentation import explain_statement_sql

    assert explain_statement_sql("SELECT * FROM devices").startswith("EXPLAIN (ANALYZE, BUFFERS) ")
    assert explain_statement_sql("  select 1").startswith("EXPLAIN (ANALYZE, BUFFERS) ")
    for write in ("UPDATE devices SET name = $1", "INSERT INTO devices VALUES ($1)", "DELETE FROM devices"):
        sql = explain_statement_sql(write)
        assert sql.startswith("EXPLAIN ")
        assert "ANALYZE" not in sql


def test_bench_explain_targets_are_top_by_total_time() -> None:
    from tests.bench_instrumentation import select_explain_targets

    tap = QueryTap()
    slow = ("app.a.slow", "SELECT device_remediation_log")
    fast = ("app.b.fast", "SELECT devices")
    unstatemented = ("app.c.nostmt", "SELECT sessions")
    tap.durations[slow].extend([50.0, 50.0])
    tap.durations[fast].append(1.0)
    tap.durations[unstatemented].append(99.0)  # no last_statement captured -> skipped
    tap.last_statement[slow] = ("SELECT * FROM device_remediation_log WHERE device_id = $1", ("x",))
    tap.last_statement[fast] = ("SELECT id FROM devices", ())

    targets = select_explain_targets(tap, top_n=2)

    assert [key for key, _stmt, _params in targets] == [slow, fast]
    assert targets[0][1] == "SELECT * FROM device_remediation_log WHERE device_id = $1"


@pytest.mark.parametrize(
    ("scenario", "devices", "churn", "present", "unhealthy"),
    [
        ("steady", 10, 0.3, 10, 3),
        ("steady", 5, 0.3, 5, 2),
        ("sparse-unhealthy", 0, 0.0, 0, 0),
        ("sparse-unhealthy", 10, 0.0, 10, 1),
        ("all-unhealthy", 10, 0.0, 10, 10),
        ("repeat-unhealthy", 10, 0.0, 10, 3),
        ("repeat-unhealthy", 10, 0.2, 10, 2),
        ("stale-ladder", 10, 0.0, 10, 0),
        ("stale-run-exclusion", 10, 0.0, 10, 0),
        ("active-claims", 10, 0.0, 10, 0),
        ("deep-history", 10, 0.0, 10, 0),
        ("terminal-noop", 10, 0.0, 5, 5),
    ],
)
def test_bench_scenario_observation_shape(
    scenario: str,
    devices: int,
    churn: float,
    present: int,
    unhealthy: int,
) -> None:
    from tests.bench_instrumentation import scenario_observation_shape

    shape = scenario_observation_shape(scenario=scenario, devices=devices, churn=churn)

    assert shape.present_count == present
    assert shape.unhealthy_count == unhealthy


def _fold_taps(
    *, source_commits: int, deferred_statements: int = 0, deferred_commits: int = 0
) -> tuple[QueryTap, CommitTap]:
    """Taps shaped like one benchmark cell's totals, with optional event-owned work."""
    tap = QueryTap()
    commits = CommitTap()
    tap.total = source_commits
    tap.callsite_counter.update(
        {("app.devices.services.connectivity.fold_host_devices", "UPDATE devices"): source_commits}
    )
    commits.count = source_commits
    commits.callsite_counter.update({"app.devices.services.connectivity.fold_host_devices": source_commits})
    if deferred_statements:
        tap.total += deferred_statements
        tap.callsite_counter.update({("app.events.event_bus.publish", "INSERT system_events"): deferred_statements})
    if deferred_commits:
        commits.count += deferred_commits
        commits.callsite_counter.update({"app.events.event_bus.publish": deferred_commits})
    return tap, commits


@pytest.mark.parametrize(
    ("scenario", "devices", "churn", "iters", "source_commits"),
    [
        # One transaction per present device, plus the fold's own inventory read.
        ("steady", 10, 0.3, 3, (10 + 1) * 3),
        ("terminal-noop", 10, 0.0, 2, (5 + 1) * 2),
    ],
)
def test_bench_fold_boundary_shape_accepts_one_transaction_per_present_device(
    scenario: str,
    devices: int,
    churn: float,
    iters: int,
    source_commits: int,
) -> None:
    from tests.bench_instrumentation import assert_fold_boundary_shape

    tap, commits = _fold_taps(source_commits=source_commits)

    assert (
        assert_fold_boundary_shape(
            tap=tap,
            commits=commits,
            scenario=scenario,
            devices=devices,
            iters=iters,
            churn=churn,
        )
        is None
    )


@pytest.mark.parametrize("source_commits", [(10 + 1) * 3 - 1, (10 + 1) * 3 + 1])
def test_bench_fold_boundary_shape_rejects_a_wrong_source_commit_count(source_commits: int) -> None:
    from tests.bench_instrumentation import assert_fold_boundary_shape

    tap, commits = _fold_taps(source_commits=source_commits)

    with pytest.raises(AssertionError, match="source transactions"):
        assert_fold_boundary_shape(tap=tap, commits=commits, scenario="steady", devices=10, iters=3, churn=0.3)


def test_bench_fold_boundary_shape_rejects_a_deferred_event_query() -> None:
    from tests.bench_instrumentation import assert_fold_boundary_shape

    tap, commits = _fold_taps(source_commits=(10 + 1) * 3, deferred_statements=2)

    with pytest.raises(AssertionError, match="deferred event statements"):
        assert_fold_boundary_shape(tap=tap, commits=commits, scenario="steady", devices=10, iters=3, churn=0.3)


def test_bench_fold_boundary_shape_accepts_the_pollers_read() -> None:
    """``dispatch_committed_events()`` runs inside the armed window on purpose.

    Its ``SELECT system_events`` is poll-delivery latency the benchmark measures,
    not a second boundary. Only an event-bus *write* or commit is deferred work.
    """
    from tests.bench_instrumentation import assert_fold_boundary_shape

    tap, commits = _fold_taps(source_commits=(10 + 1) * 3)
    tap.total += 3
    tap.callsite_counter.update({("app.events.event_bus._scan_window", "SELECT system_events"): 3})

    assert (
        assert_fold_boundary_shape(tap=tap, commits=commits, scenario="steady", devices=10, iters=3, churn=0.3) is None
    )


def test_bench_fold_boundary_shape_rejects_a_deferred_event_commit() -> None:
    from tests.bench_instrumentation import assert_fold_boundary_shape

    tap, commits = _fold_taps(source_commits=(10 + 1) * 3, deferred_commits=1)

    with pytest.raises(AssertionError, match="deferred event commits"):
        assert_fold_boundary_shape(tap=tap, commits=commits, scenario="steady", devices=10, iters=3, churn=0.3)
