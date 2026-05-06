"""Unit tests for assert_current_leader fencing helper."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from app.services.control_plane_leader import (
    LeadershipLost,
    assert_current_leader,
    control_plane_leader,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


@pytest.mark.db
@pytest.mark.asyncio
async def test_assert_current_leader_allows_matching_holder(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    assert await control_plane_leader.try_acquire(setup_database)
    try:
        await assert_current_leader(db_session)
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
            await assert_current_leader(db_session)
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
            await assert_current_leader(db_session)
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
            await assert_current_leader(db_session)
    finally:
        await control_plane_leader.release()


@pytest.mark.db
@pytest.mark.asyncio
async def test_assert_current_leader_noop_when_keepalive_disabled(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert await control_plane_leader.try_acquire(setup_database)
    try:
        await db_session.execute(
            text("UPDATE control_plane_leader_heartbeats SET holder_id = :other WHERE id = 1"),
            {"other": str(uuid.uuid4())},
        )
        await db_session.commit()

        from app.services import control_plane_leader as module

        monkeypatch.setattr(
            module.settings_service,
            "get",
            lambda key: False if key == "general.leader_keepalive_enabled" else module.settings_service.get(key),
        )
        with caplog.at_level(logging.DEBUG, logger="app.services.control_plane_leader"):
            await assert_current_leader(db_session)
        assert any("fencing_disabled" in record.message for record in caplog.records)
    finally:
        await control_plane_leader.release()
