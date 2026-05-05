from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.control_plane_leader import ControlPlaneLeader


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


async def test_try_acquire_releases_preempted_lock_when_heartbeat_claim_fails() -> None:
    leader = ControlPlaneLeader()
    connection = AsyncMock()
    initial_result = SimpleNamespace(scalar=lambda: 0)
    retry_result = SimpleNamespace(scalar=lambda: 1)
    connection.execute.side_effect = [initial_result, retry_result]
    engine = AsyncMock()
    engine.connect.return_value = connection
    leader._try_preempt = AsyncMock(return_value=True)  # type: ignore[method-assign]
    leader._claim_heartbeat_row = AsyncMock(side_effect=RuntimeError("claim failed"))  # type: ignore[method-assign]

    try:
        try:
            await leader.try_acquire(engine, stale_threshold_sec=30)
        except RuntimeError as exc:
            assert str(exc) == "claim failed"
        else:
            raise AssertionError("expected heartbeat claim failure")
    finally:
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
