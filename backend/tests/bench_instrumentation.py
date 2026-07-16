"""Query/commit taps and the async-session call-site profiler for the fold benchmarks.

Non-test helper module so the pure-logic pieces are unit-testable in the normal
CI suite (tests/test_bench_instrumentation.py) while the DB-bound benchmarks in
tests/test_bench_folds.py stay behind FOLD_BENCH=1.
"""

from __future__ import annotations

import asyncio
import functools
import re
import sys
from collections import Counter
from contextvars import ContextVar
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


class QueryTap:
    def __init__(self) -> None:
        self.counter: Counter[str] = Counter()
        self.callsite_counter: Counter[tuple[str, str]] = Counter()
        self.total = 0
        self.armed = True

    def __call__(self, conn: object, cursor: object, statement: str, *a: object) -> None:
        if not self.armed:
            return
        self.total += 1
        signature = statement_signature(statement)
        self.counter[signature] += 1
        self.callsite_counter[(ACTIVE_DB_CALLSITE.get(), signature)] += 1

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
