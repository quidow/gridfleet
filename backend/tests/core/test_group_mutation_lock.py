"""The group-mutation advisory lock must actually exclude a second transaction."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from app.core.locks import acquire_group_mutation_lock

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
