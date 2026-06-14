"""Non-leader watcher behavior."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text

from app.core.leader.advisory import ControlPlaneLeader
from app.core.leader.watcher import run_watcher_once

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


def _mock_settings(**kwargs: object) -> MagicMock:
    defaults: dict[str, object] = {
        "general.leader_keepalive_enabled": True,
        "general.leader_keepalive_interval_sec": 5,
        "general.leader_stale_threshold_sec": 30,
    }
    defaults.update(kwargs)
    mock = MagicMock()
    mock.get = lambda key: defaults[key]  # type: ignore[return-value]
    mock.get_int = lambda key: int(defaults[key])  # type: ignore[call-overload]
    mock.get_float = lambda key: float(defaults[key])  # type: ignore[arg-type]
    mock.get_bool = lambda key: bool(defaults[key])
    return mock


@pytest.mark.db
@pytest.mark.asyncio
async def test_watcher_no_op_when_heartbeat_fresh(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    _ = db_session
    leader = ControlPlaneLeader()
    assert await leader.try_acquire(setup_database)
    try:
        non_leader = ControlPlaneLeader()
        await run_watcher_once(non_leader, engine=setup_database, settings=_mock_settings())
        assert non_leader._connection is None
    finally:
        await leader.release()


@pytest.mark.db
@pytest.mark.asyncio
async def test_watcher_preempts_and_exits(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    leader = ControlPlaneLeader()
    assert await leader.try_acquire(setup_database)
    try:
        await db_session.execute(
            text("UPDATE control_plane_leader_heartbeats SET last_heartbeat_at = :ts WHERE id = 1"),
            {"ts": datetime.now(UTC) - timedelta(seconds=300)},
        )
        await db_session.commit()

        non_leader = ControlPlaneLeader()
        with patch(
            "app.core.leader.watcher._exit_after_preempt",
            new_callable=AsyncMock,
        ) as exit_stub:
            await run_watcher_once(non_leader, engine=setup_database, settings=_mock_settings())
        exit_stub.assert_awaited_once()
        assert non_leader._connection is not None
        await non_leader.release()
    finally:
        with contextlib.suppress(Exception):
            await leader.release()


@pytest.mark.asyncio
async def test_watcher_does_not_preempt_when_disabled() -> None:
    non_leader = ControlPlaneLeader()
    non_leader.try_acquire = AsyncMock(return_value=False)  # type: ignore[method-assign]
    await run_watcher_once(
        non_leader,
        engine=None,
        settings=_mock_settings(**{"general.leader_keepalive_enabled": False}),
    )
    non_leader.try_acquire.assert_not_called()
