"""The group-mutation advisory lock must actually exclude a second transaction."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.core.locks import acquire_group_mutation_lock, group_mutation_lock

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

# Long enough for the second transaction's blocked acquire to reach the lock
# manager before the holder commits. Only widens the window; correctness does
# not depend on the exact value.
_HANDOFF_SEC = 0.5


async def test_group_mutation_lock_serialises_transactions(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    order: list[str] = []
    holder_acquired = asyncio.Event()

    async def holder() -> None:
        async with db_session_maker() as session:
            await acquire_group_mutation_lock(session)
            holder_acquired.set()
            await asyncio.sleep(_HANDOFF_SEC)
            order.append("holder-commit")
            await session.commit()

    async def waiter() -> None:
        await holder_acquired.wait()
        async with db_session_maker() as session:
            await acquire_group_mutation_lock(session)
            order.append("waiter-acquire")
            await session.commit()

    await asyncio.gather(holder(), waiter())

    assert order == ["holder-commit", "waiter-acquire"], (
        f"waiter acquired the lock before the holder committed: {order}"
    )


async def test_group_mutation_lock_released_on_rollback(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Transaction scope means an aborted writer must not strand the lock."""
    async with db_session_maker() as first:
        await acquire_group_mutation_lock(first)
        await first.rollback()

    async with db_session_maker() as second:
        await asyncio.wait_for(acquire_group_mutation_lock(second), timeout=5.0)
        await second.commit()


async def test_rollback_failure_does_not_displace_the_body_exception() -> None:
    """A cleanup failure must never replace what the body was raising.

    ``update_group``/``delete_group`` signal rejection by raising typed errors
    the routers map to 422 and 409. If the scope's ``finally`` rollback raises —
    dropped connection, cancelled task — Python discards the in-flight exception
    in favour of the cleanup one, and a correctly-rejected payload comes back as
    an opaque 500.
    """

    class _BodyError(Exception):
        pass

    session = SimpleNamespace(
        execute=AsyncMock(),
        in_transaction=lambda: True,
        rollback=AsyncMock(side_effect=RuntimeError("connection is closed")),
    )

    with pytest.raises(_BodyError):
        async with group_mutation_lock(session):  # type: ignore[arg-type]
            raise _BodyError

    session.rollback.assert_awaited_once()
