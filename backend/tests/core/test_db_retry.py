from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, cast

import pytest
from prometheus_client import REGISTRY
from sqlalchemy.exc import DBAPIError

from app.core.db_retry import is_retryable_serialization_error, retry_on_serialization_failure

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SessionFactory


def _dbapi_error(sqlstate: str) -> DBAPIError:
    """Build a DBAPIError whose ``.orig`` carries a Postgres sqlstate, mirroring
    the asyncpg-wrapped errors the helper must classify."""

    class _OrigError(Exception):
        def __init__(self, code: str) -> None:
            super().__init__(code)
            self.sqlstate = code

    return DBAPIError("stmt", {}, _OrigError(sqlstate))


# Alias used by the fresh-session retry tests below.
retryable_dbapi_error = _dbapi_error


def test_is_retryable_detects_deadlock_and_serialization() -> None:
    assert is_retryable_serialization_error(_dbapi_error("40P01")) is True
    assert is_retryable_serialization_error(_dbapi_error("40001")) is True
    assert is_retryable_serialization_error(_dbapi_error("23505")) is False


async def _yield(db: AsyncSession) -> AsyncIterator[AsyncSession]:
    yield db


class _RecordingFactory:
    """Session factory double that records every fresh session opened by ``begin``."""

    sessions: list[object]

    def __init__(self) -> None:
        self.sessions = []

    def begin(self) -> AbstractAsyncContextManager[AsyncSession]:
        db = cast("AsyncSession", object())
        self.sessions.append(db)
        return asynccontextmanager(_yield)(db)


async def test_retry_opens_a_fresh_transaction_for_each_attempt() -> None:
    sessions: list[object] = []
    calls = 0

    class Factory:
        def begin(self) -> AbstractAsyncContextManager[AsyncSession]:
            db = cast("AsyncSession", object())
            sessions.append(db)
            return asynccontextmanager(_yield)(db)

    async def attempt(db: AsyncSession) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise retryable_dbapi_error("40001")
        assert db is sessions[-1]
        return "ok"

    assert (
        await retry_on_serialization_failure(cast("SessionFactory", Factory()), attempt, caller="test", backoff_sec=0)
        == "ok"
    )
    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]


async def test_retry_does_not_swallow_non_serialization_errors() -> None:
    factory = _RecordingFactory()
    raised: list[DBAPIError] = []

    async def attempt(db: AsyncSession) -> str:
        err = _dbapi_error("23505")  # unique violation — not retryable
        raised.append(err)
        raise err

    with pytest.raises(DBAPIError) as exc:
        await retry_on_serialization_failure(cast("SessionFactory", factory), attempt, caller="test", backoff_sec=0)
    assert exc.value is raised[0]  # original exception propagates unchanged
    assert len(factory.sessions) == 1


async def test_retry_surfaces_error_after_exhausting_attempts() -> None:
    factory = _RecordingFactory()

    async def attempt(db: AsyncSession) -> str:
        raise retryable_dbapi_error("40P01")

    with pytest.raises(DBAPIError):
        await retry_on_serialization_failure(
            cast("SessionFactory", factory), attempt, caller="exhausted-test", attempts=3, backoff_sec=0
        )
    assert len(factory.sessions) == 3
    assert len({id(s) for s in factory.sessions}) == 3
    assert (
        REGISTRY.get_sample_value("db_serialization_retry_total", {"caller": "exhausted-test", "outcome": "exhausted"})
        == 1.0
    )
