"""Query/commit taps and the async-session call-site profiler for the fold benchmarks.

Non-test helper module so the pure-logic pieces are unit-testable in the normal
CI suite (tests/test_bench_instrumentation.py) while the DB-bound benchmarks in
tests/test_bench_folds.py stay behind FOLD_BENCH=1.
"""

from __future__ import annotations

import asyncio
import functools
import math
import re
import sys
from collections import Counter, defaultdict
from contextvars import ContextVar
from time import perf_counter
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import FrameType
    from typing import Concatenate

    import pytest

_WS = re.compile(r"\s+")
ACTIVE_DB_CALLSITE: ContextVar[str] = ContextVar("fold_bench_db_callsite", default="unattributed")
_ACTIVE_DB_TASK: ContextVar[asyncio.Task[object] | None] = ContextVar("fold_bench_db_task", default=None)
DEFERRED_EVENT_CALLSITE = "app.events.event_bus._persist_system_event"


def callsite_label(frame: FrameType) -> str:
    normalized = frame.f_code.co_filename.replace("\\", "/")
    relative = normalized.split("/backend/", maxsplit=1)[-1]
    module = relative.removesuffix(".py").replace("/", ".")
    return f"{module}.{frame.f_code.co_name}"


def profiled_async_session_method[**P, R](
    method: Callable[Concatenate[AsyncSession, P], Awaitable[R]],
) -> Callable[Concatenate[AsyncSession, P], Awaitable[R]]:
    """Carry an application call site through SQLAlchemy's async greenlet.

    Nested convenience calls such as ``scalars() -> execute()`` keep the outer
    application label. A newly-created task gets a fresh label even though
    ``asyncio.create_task`` copied the parent's ContextVar values.
    """

    @functools.wraps(method)
    async def wrapped(
        session: AsyncSession,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        task = asyncio.current_task()
        current = ACTIVE_DB_CALLSITE.get()
        inherited_by_child = _ACTIVE_DB_TASK.get() is not task
        label_token = (
            ACTIVE_DB_CALLSITE.set(callsite_label(sys._getframe(1)))
            if current == "unattributed" or inherited_by_child
            else None
        )
        task_token = _ACTIVE_DB_TASK.set(task) if label_token is not None else None
        try:
            return await method(session, *args, **kwargs)
        finally:
            if task_token is not None:
                _ACTIVE_DB_TASK.reset(task_token)
            if label_token is not None:
                ACTIVE_DB_CALLSITE.reset(label_token)

    return wrapped


def install_async_session_callsite_profiler(monkeypatch: pytest.MonkeyPatch) -> None:
    for method_name in ("commit", "execute", "flush", "get", "refresh", "scalar", "scalars"):
        original = getattr(AsyncSession, method_name)
        monkeypatch.setattr(AsyncSession, method_name, profiled_async_session_method(original))


def is_deferred_event_callsite(callsite: str) -> bool:
    return callsite == DEFERRED_EVENT_CALLSITE


def statement_signature(sql: str) -> str:
    """Collapse a statement to verb + first table so round-trips group by kind."""
    s = _WS.sub(" ", sql.strip())
    m = re.match(r"(?i)(SELECT|INSERT INTO|UPDATE|DELETE FROM)\s+([^\s(]+)?", s)
    if not m:
        return s[:48]
    verb = m.group(1).upper().split()[0]
    if verb == "SELECT":
        tbl = re.search(r"(?i)\bFROM\s+([^\s(]+)", s)
        return f"SELECT {tbl.group(1) if tbl else '?'}"
    return f"{verb} {m.group(2) or '?'}"


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile. Returns 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


class QueryTap:
    """before/after_cursor_execute listener pair.

    ``__call__`` is the ``before_cursor_execute`` hook (keeps the historical
    3-positional-arg call shape working); ``after`` is the optional
    ``after_cursor_execute`` hook that adds duration/rows/last-statement
    capture. Timing state rides on the SQLAlchemy execution context so
    concurrent cursors cannot cross-talk.
    """

    def __init__(self) -> None:
        self.counter: Counter[str] = Counter()
        self.callsite_counter: Counter[tuple[str, str]] = Counter()
        self.total = 0
        self.armed = True
        self.durations: dict[tuple[str, str], list[float]] = defaultdict(list)
        self.rows: Counter[tuple[str, str]] = Counter()
        self.last_statement: dict[tuple[str, str], tuple[str, object]] = {}

    def __call__(
        self,
        conn: object,
        cursor: object,
        statement: str,
        parameters: object = None,
        context: object = None,
        executemany: bool = False,
    ) -> None:
        if not self.armed:
            return
        self.total += 1
        signature = statement_signature(statement)
        self.counter[signature] += 1
        key = (ACTIVE_DB_CALLSITE.get(), signature)
        self.callsite_counter[key] += 1
        if context is not None:
            context._fold_bench_t0 = perf_counter()  # type: ignore[attr-defined]
            context._fold_bench_key = key  # type: ignore[attr-defined]

    def after(
        self,
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if not self.armed or context is None:
            return
        t0 = getattr(context, "_fold_bench_t0", None)
        key = getattr(context, "_fold_bench_key", None)
        if t0 is None or key is None:
            return
        del context._fold_bench_t0  # a later re-execute on the same context must re-arm
        del context._fold_bench_key
        self.durations[key].append((perf_counter() - t0) * 1000)
        rowcount = getattr(cursor, "rowcount", -1)
        if isinstance(rowcount, int) and rowcount > 0:
            self.rows[key] += rowcount
        self.last_statement[key] = (statement, parameters)

    @property
    def deferred_total(self) -> int:
        return sum(
            count
            for (callsite, _signature_name), count in self.callsite_counter.items()
            if is_deferred_event_callsite(callsite)
        )

    @property
    def source_total(self) -> int:
        return self.total - self.deferred_total


def explain_statement_sql(statement: str) -> str:
    """EXPLAIN wrapper: ANALYZE only for SELECTs — an ANALYZE'd write would execute it."""
    if statement.lstrip().upper().startswith("SELECT"):
        return f"EXPLAIN (ANALYZE, BUFFERS) {statement}"
    return f"EXPLAIN {statement}"


def select_explain_targets(tap: QueryTap, top_n: int = 8) -> list[tuple[tuple[str, str], str, object]]:
    """Top call sites by cumulative statement time that captured a statement.
    ``top_n`` bounds the *result count*: call sites without a captured statement
    are skipped and backfilled from the next-ranked candidate rather than
    dropping a slot."""
    ranked = sorted(tap.durations.items(), key=lambda kv: sum(kv[1]), reverse=True)
    targets: list[tuple[tuple[str, str], str, object]] = []
    for key, _durations in ranked:
        captured = tap.last_statement.get(key)
        if captured is None:
            continue
        statement, parameters = captured
        targets.append((key, statement, parameters))
        if len(targets) >= top_n:
            break
    return targets


def build_json_report(
    *,
    config: dict[str, object],
    tap: QueryTap,
    commits: CommitTap,
    iters: int,
    fold_wall_ms: list[float],
    settled_wall_ms: list[float],
    explain_plans: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """Machine-readable single-cell result for the sweep script. Per-fold values
    divide by *iters* (armed iterations only)."""

    def wall(values: list[float]) -> dict[str, object]:
        return {"median": percentile(values, 0.5), "p95": percentile(values, 0.95), "all": values}

    callsites = [
        {
            "callsite": callsite,
            "signature": signature,
            "calls_per_fold": tap.callsite_counter[(callsite, signature)] / iters,
            "total_ms_per_fold": sum(durations) / iters,
            "median_ms": percentile(durations, 0.5),
            "p95_ms": percentile(durations, 0.95),
            "rows_per_fold": tap.rows[(callsite, signature)] / iters,
        }
        for (callsite, signature), durations in sorted(tap.durations.items(), key=lambda kv: sum(kv[1]), reverse=True)
    ]
    return {
        "config": config,
        "wall_ms": {"fold_return": wall(fold_wall_ms), "event_settled": wall(settled_wall_ms)},
        "queries": {
            "source_per_fold": tap.source_total / iters,
            "deferred_per_fold": tap.deferred_total / iters,
            "complete_per_fold": tap.total / iters,
        },
        "commits": {
            "source_per_fold": commits.source_count / iters,
            "deferred_per_fold": commits.deferred_count / iters,
            "complete_per_fold": commits.count / iters,
        },
        "signatures": {signature: count / iters for signature, count in tap.counter.most_common()},
        "callsites": callsites,
        "explain": explain_plans or [],
    }


class CommitTap:
    def __init__(self) -> None:
        self.callsite_counter: Counter[str] = Counter()
        self.count = 0
        self.armed = True

    def __call__(self, conn: object) -> None:
        if self.armed:
            self.count += 1
            self.callsite_counter[ACTIVE_DB_CALLSITE.get()] += 1

    @property
    def deferred_count(self) -> int:
        return sum(count for callsite, count in self.callsite_counter.items() if is_deferred_event_callsite(callsite))

    @property
    def source_count(self) -> int:
        return self.count - self.deferred_count
