"""Unit tests for assert_current_leader fencing helper."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from app.core.leader.advisory import (
    LeadershipLost,
    assert_current_leader,
    control_plane_leader,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


def _mock_settings(**kwargs: object) -> MagicMock:
    defaults: dict[str, object] = {
        "general.leader_keepalive_enabled": True,
    }
    defaults.update(kwargs)
    mock = MagicMock()
    mock.get = lambda key: defaults[key]
    return mock


@pytest.mark.db
@pytest.mark.asyncio
async def test_assert_current_leader_allows_matching_holder(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    assert await control_plane_leader.try_acquire(setup_database)
    try:
        await assert_current_leader(db_session, settings=_mock_settings())
    finally:
        await control_plane_leader.release()


@pytest.mark.db
@pytest.mark.asyncio
async def test_assert_current_leader_raises_when_holder_mismatched(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    assert await control_plane_leader.try_acquire(setup_database)
    try:
        await db_session.execute(
            text("UPDATE control_plane_leader_heartbeats SET holder_id = :other WHERE id = 1"),
            {"other": str(uuid.uuid4())},
        )
        await db_session.commit()
        with pytest.raises(LeadershipLost):
            await assert_current_leader(db_session, settings=_mock_settings())
    finally:
        await control_plane_leader.release()


@pytest.mark.db
@pytest.mark.asyncio
async def test_assert_current_leader_raises_when_lock_backend_pid_missing(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    assert await control_plane_leader.try_acquire(setup_database)
    try:
        await db_session.execute(
            text("UPDATE control_plane_leader_heartbeats SET lock_backend_pid = NULL WHERE id = 1"),
        )
        await db_session.commit()
        with pytest.raises(LeadershipLost):
            await assert_current_leader(db_session, settings=_mock_settings())
    finally:
        await control_plane_leader.release()


@pytest.mark.db
@pytest.mark.asyncio
async def test_assert_current_leader_raises_when_row_absent(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    assert await control_plane_leader.try_acquire(setup_database)
    try:
        await db_session.execute(text("DELETE FROM control_plane_leader_heartbeats WHERE id = 1"))
        await db_session.commit()
        with pytest.raises(LeadershipLost):
            await assert_current_leader(db_session, settings=_mock_settings())
    finally:
        await control_plane_leader.release()


@pytest.mark.db
@pytest.mark.asyncio
async def test_assert_current_leader_noop_when_keepalive_disabled(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert await control_plane_leader.try_acquire(setup_database)
    try:
        await db_session.execute(
            text("UPDATE control_plane_leader_heartbeats SET holder_id = :other WHERE id = 1"),
            {"other": str(uuid.uuid4())},
        )
        await db_session.commit()

        with caplog.at_level(logging.DEBUG, logger="app.core.leader.advisory"):
            await assert_current_leader(
                db_session, settings=_mock_settings(**{"general.leader_keepalive_enabled": False})
            )
        assert any("fencing_disabled" in record.message for record in caplog.records)
    finally:
        await control_plane_leader.release()
