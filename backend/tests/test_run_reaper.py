from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_run import RunState, TestRun
from app.services.run_reaper import _reap_stale_runs


async def test_reap_stale_runs_expires_heartbeat_timeout(db_session: AsyncSession) -> None:
    stale_run = TestRun(
        name="Heartbeat Timeout",
        created_by="qa",
        state=RunState.preparing,
        requirements=[],
        last_heartbeat=datetime.now(UTC) - timedelta(seconds=61),
        heartbeat_timeout_sec=60,
        ttl_minutes=60,
    )
    db_session.add(stale_run)
    await db_session.commit()

    with patch("app.services.run_reaper.run_service.expire_run", new_callable=AsyncMock) as expire_run:
        await _reap_stale_runs(db_session)

    expire_run.assert_awaited_once()
    assert expire_run.await_args is not None
    assert expire_run.await_args.args[1].id == stale_run.id
    assert expire_run.await_args.args[2] == "Heartbeat timeout"


async def test_reap_stale_runs_expires_ttl(db_session: AsyncSession) -> None:
    stale_run = TestRun(
        name="TTL Timeout",
        created_by="qa",
        state=RunState.ready,
        requirements=[],
        created_at=datetime.now(UTC) - timedelta(minutes=31),
        ttl_minutes=30,
        heartbeat_timeout_sec=600,
    )
    db_session.add(stale_run)
    await db_session.commit()

    with patch("app.services.run_reaper.run_service.expire_run", new_callable=AsyncMock) as expire_run:
        await _reap_stale_runs(db_session)

    expire_run.assert_awaited_once()
    assert expire_run.await_args is not None
    assert expire_run.await_args.args[1].id == stale_run.id
    assert expire_run.await_args.args[2] == "TTL exceeded (30 minutes)"


async def test_reap_stale_runs_ignores_terminal_and_fresh_runs(db_session: AsyncSession) -> None:
    completed_run = TestRun(
        name="Completed",
        created_by="qa",
        state=RunState.completed,
        requirements=[],
        last_heartbeat=datetime.now(UTC) - timedelta(days=1),
        created_at=datetime.now(UTC) - timedelta(days=1),
        ttl_minutes=1,
        heartbeat_timeout_sec=1,
    )
    fresh_run = TestRun(
        name="Fresh",
        created_by="qa",
        state=RunState.active,
        requirements=[],
        last_heartbeat=datetime.now(UTC),
        created_at=datetime.now(UTC),
        ttl_minutes=60,
        heartbeat_timeout_sec=60,
    )
    db_session.add_all([completed_run, fresh_run])
    await db_session.commit()

    with patch("app.services.run_reaper.run_service.expire_run", new_callable=AsyncMock) as expire_run:
        await _reap_stale_runs(db_session)

    expire_run.assert_not_awaited()
