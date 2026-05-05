"""Fencing semantics: heartbeat write that does not match holder_id is fatal.

Tests use the fixture's per-test engine (`setup_database`), not the global
`app.database.engine` - the test fixture creates a unique schema per test
and binds the engine to a `search_path` for that schema. Using the global
engine would touch the wrong schema or fail outright.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock as _AsyncMock

import pytest
from sqlalchemy import text

from app.models.control_plane_leader_heartbeat import ControlPlaneLeaderHeartbeat
from app.services.control_plane_leader import ControlPlaneLeader, LeadershipLost

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


@pytest.mark.db
@pytest.mark.asyncio
async def test_acquire_refreshes_heartbeat_with_self_holder(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    leader = ControlPlaneLeader()
    try:
        assert await leader.try_acquire(setup_database)
        row = await db_session.get(ControlPlaneLeaderHeartbeat, 1)
        assert row is not None
        assert row.holder_id == leader.holder_id
    finally:
        await leader.release()


@pytest.mark.db
@pytest.mark.asyncio
async def test_write_heartbeat_returning_empty_raises_leadership_lost(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    leader = ControlPlaneLeader()
    assert await leader.try_acquire(setup_database)
    try:
        await db_session.execute(
            text("UPDATE control_plane_leader_heartbeats SET holder_id = :other WHERE id = 1"),
            {"other": str(uuid.uuid4())},
        )
        await db_session.commit()

        with pytest.raises(LeadershipLost):
            await leader.write_heartbeat()
    finally:
        await leader.release()


@pytest.mark.asyncio
async def test_write_heartbeat_raises_leadership_lost_when_lock_connection_dead() -> None:
    leader = ControlPlaneLeader()
    fake_conn = _AsyncMock()
    fake_conn.execute.side_effect = RuntimeError("connection lost")
    leader._connection = fake_conn

    with pytest.raises(LeadershipLost):
        await leader.write_heartbeat()


@pytest.mark.asyncio
async def test_write_heartbeat_raises_leadership_lost_when_no_connection() -> None:
    leader = ControlPlaneLeader()
    assert leader._connection is None
    with pytest.raises(LeadershipLost):
        await leader.write_heartbeat()


@pytest.mark.db
@pytest.mark.asyncio
async def test_preempt_when_heartbeat_stale_via_force_unlock(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    """Stale heartbeat -> terminate holder backend -> re-acquire."""
    other = ControlPlaneLeader()
    assert await other.try_acquire(setup_database)
    try:
        await db_session.execute(
            text("UPDATE control_plane_leader_heartbeats SET holder_id = :h, last_heartbeat_at = :ts WHERE id = 1"),
            {
                "h": str(other.holder_id),
                "ts": datetime.now(UTC) - timedelta(seconds=300),
            },
        )
        await db_session.commit()

        new_leader = ControlPlaneLeader()
        try:
            acquired = await new_leader.try_acquire(setup_database, stale_threshold_sec=30)
            assert acquired
            await db_session.commit()
            row = await db_session.get(ControlPlaneLeaderHeartbeat, 1)
            assert row is not None
            await db_session.refresh(row)
            assert row.holder_id == new_leader.holder_id
        finally:
            await new_leader.release()
    finally:
        with contextlib.suppress(Exception):
            await other.release()
