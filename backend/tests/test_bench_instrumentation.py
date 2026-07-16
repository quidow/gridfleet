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
