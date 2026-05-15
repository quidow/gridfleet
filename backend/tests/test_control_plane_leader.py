from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core.leader.advisory import ControlPlaneLeader


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


class _BeginContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


async def test_preempt_skips_fresh_missing_pid_and_changed_heartbeat_rows() -> None:
    leader = ControlPlaneLeader()
    connection = AsyncMock()
    connection.execute.return_value = SimpleNamespace(first=lambda: None)
    assert await leader._try_preempt(connection, stale_threshold_sec=30) is False

    connection.execute.return_value = SimpleNamespace(
        first=lambda: SimpleNamespace(age=60, lock_backend_pid=None, holder_id=uuid.uuid4())
    )
    assert await leader._try_preempt(connection, stale_threshold_sec=30) is False

    connection.begin = _BeginContext
    connection.execute.side_effect = [
        SimpleNamespace(first=lambda: SimpleNamespace(age=60, lock_backend_pid=123, holder_id=uuid.uuid4())),
        SimpleNamespace(first=lambda: None),
    ]
    assert await leader._try_preempt(connection, stale_threshold_sec=30) is False


async def test_preempt_warns_once_when_termination_does_not_grant_lock() -> None:
    leader = ControlPlaneLeader()
    connection = AsyncMock()
    connection.begin = _BeginContext
    holder_id = uuid.uuid4()
    connection.execute.side_effect = [
        SimpleNamespace(first=lambda: SimpleNamespace(age=60, lock_backend_pid=123, holder_id=holder_id)),
        SimpleNamespace(first=object),
        SimpleNamespace(scalar=lambda: False),
        SimpleNamespace(scalar=lambda: False),
    ]

    assert await leader._try_preempt(connection, stale_threshold_sec=30) is False
    assert leader._privilege_warned is True
