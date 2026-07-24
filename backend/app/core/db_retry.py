"""Bounded retry for transient Postgres serialization failures.

A transaction that locks several rows (device + session + ticket) and races
concurrent teardown / reconcile traffic on the same rows can lose a Postgres
lock cycle and be aborted with sqlstate 40P01 (deadlock) or 40001
(serialization failure). The loser's work is rolled back atomically, so the
correct response is to open a fresh transaction and re-run the whole thing a
bounded number of times rather than surface a 500. This helper promotes the
pattern that previously lived only in app/runs/service_lifecycle.py so every
serialization-prone transaction can share it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sqlalchemy.exc import DBAPIError

from app.core.errors import pg_sqlstate
from app.core.metrics_recorders import DB_SERIALIZATION_RETRY_TOTAL

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SessionFactory

_PG_DEADLOCK_SQLSTATE = "40P01"
_PG_SERIALIZATION_FAILURE_SQLSTATE = "40001"
_RETRYABLE_SQLSTATES = frozenset({_PG_DEADLOCK_SQLSTATE, _PG_SERIALIZATION_FAILURE_SQLSTATE})

_DEFAULT_ATTEMPTS = 3
_DEFAULT_BACKOFF_SEC = 0.1


def is_retryable_serialization_error(exc: DBAPIError) -> bool:
    """True when the DBAPI error chain carries a retryable Postgres sqlstate."""
    return pg_sqlstate(exc) in _RETRYABLE_SQLSTATES


async def retry_on_serialization_failure[T](
    session_factory: SessionFactory,
    attempt_txn: Callable[[AsyncSession], Awaitable[T]],
    *,
    caller: str,
    attempts: int = _DEFAULT_ATTEMPTS,
    backoff_sec: float = _DEFAULT_BACKOFF_SEC,
) -> T:
    n = 0
    while True:
        try:
            async with session_factory.begin() as db:
                return await attempt_txn(db)
        except DBAPIError as exc:
            if not is_retryable_serialization_error(exc):
                raise
            n += 1
            if n >= attempts:
                DB_SERIALIZATION_RETRY_TOTAL.labels(caller=caller, outcome="exhausted").inc()
                raise
            DB_SERIALIZATION_RETRY_TOTAL.labels(caller=caller, outcome="retried").inc()
            await asyncio.sleep(backoff_sec)
