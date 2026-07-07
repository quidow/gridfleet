from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.core.leader.advisory import ControlPlaneLeader

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


async def test_try_acquire_returns_true_when_connection_already_held() -> None:
    leader = ControlPlaneLeader()
    leader._connection = AsyncMock()

    assert await leader.try_acquire(AsyncMock()) is True


async def test_try_acquire_stores_connection_when_lock_acquired() -> None:
    leader = ControlPlaneLeader()
    connection = AsyncMock()
    result = SimpleNamespace(scalar=lambda: 1)
    connection.execute.return_value = result
    engine = AsyncMock()
    engine.connect.return_value = connection

    acquired = await leader.try_acquire(engine)

    assert acquired is True
    assert leader._connection is connection
    connection.close.assert_not_awaited()


async def test_try_acquire_closes_connection_when_lock_not_acquired() -> None:
    leader = ControlPlaneLeader()
    connection = AsyncMock()
    result = SimpleNamespace(scalar=lambda: 0)
    connection.execute.return_value = result
    engine = AsyncMock()
    engine.connect.return_value = connection

    acquired = await leader.try_acquire(engine)

    assert acquired is False
    assert leader._connection is None
    connection.close.assert_awaited_once()


async def test_release_noops_without_connection() -> None:
    leader = ControlPlaneLeader()
    await leader.release()


async def test_release_swallows_unlock_failure_and_closes_connection() -> None:
    leader = ControlPlaneLeader()
    connection = AsyncMock()
    connection.execute.side_effect = RuntimeError("unlock failed")
    leader._connection = connection

    await leader.release()

    connection.close.assert_awaited_once()
    assert leader._connection is None


@pytest.mark.db
async def test_second_process_does_not_acquire(setup_database: AsyncEngine) -> None:
    # The residual advisory lock is the accidental-double-launch guard: with one
    # holder the lock is held, a second process fails to acquire, and it frees on
    # release so a restarted process can re-acquire.
    first = ControlPlaneLeader()
    second = ControlPlaneLeader()
    assert await first.try_acquire(setup_database) is True
    assert await second.try_acquire(setup_database) is False
    await first.release()
    assert await second.try_acquire(setup_database) is True
    await second.release()
