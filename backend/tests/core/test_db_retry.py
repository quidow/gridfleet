from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import DBAPIError

from app.core.db_retry import is_retryable_serialization_error, retry_on_serialization_failure


def _dbapi_error(sqlstate: str) -> DBAPIError:
    """Build a DBAPIError whose ``.orig`` carries a Postgres sqlstate, mirroring
    the asyncpg-wrapped errors the helper must classify."""

    class _OrigError(Exception):
        def __init__(self, code: str) -> None:
            super().__init__(code)
            self.sqlstate = code

    return DBAPIError("stmt", {}, _OrigError(sqlstate))


def test_is_retryable_detects_deadlock_and_serialization() -> None:
    assert is_retryable_serialization_error(_dbapi_error("40P01")) is True
    assert is_retryable_serialization_error(_dbapi_error("40001")) is True
    assert is_retryable_serialization_error(_dbapi_error("23505")) is False


async def test_retry_reruns_on_deadlock_then_succeeds() -> None:
    db = AsyncMock()
    calls = {"n": 0}

    async def attempt() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _dbapi_error("40P01")
        return "ok"

    result = await retry_on_serialization_failure(db, attempt, caller="test")

    assert result == "ok"
    assert calls["n"] == 2
    db.rollback.assert_awaited_once()


async def test_retry_does_not_swallow_non_serialization_errors() -> None:
    db = AsyncMock()

    async def attempt() -> str:
        raise _dbapi_error("23505")  # unique violation — not retryable

    with pytest.raises(DBAPIError):
        await retry_on_serialization_failure(db, attempt, caller="test")
    db.rollback.assert_not_awaited()


async def test_retry_surfaces_error_after_exhausting_attempts() -> None:
    db = AsyncMock()
    calls = {"n": 0}

    async def attempt() -> str:
        calls["n"] += 1
        raise _dbapi_error("40P01")

    with pytest.raises(DBAPIError):
        await retry_on_serialization_failure(db, attempt, caller="test", attempts=3, backoff_sec=0.0)
    assert calls["n"] == 3
