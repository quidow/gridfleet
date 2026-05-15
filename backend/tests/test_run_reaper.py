from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.runs import service_lifecycle_release as run_lifecycle_release
from app.runs.models import RunState, TestRun
from app.runs.service_reaper import _reap_stale_runs


@pytest.fixture(autouse=True)
def _skip_leader_fencing() -> Iterator[None]:
    with patch("app.runs.service_reaper.assert_current_leader"):
        yield


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

    with patch("app.runs.service_reaper.run_service.expire_run", new_callable=AsyncMock) as expire_run:
        await _reap_stale_runs(db_session)

    expire_run.assert_awaited_once()
    assert expire_run.await_args is not None
    assert expire_run.await_args.args[1].id == stale_run.id
    assert expire_run.await_args.args[2] == "Heartbeat timeout"


async def test_reap_stale_runs_expires_ttl(db_session: AsyncSession) -> None:
    stale_run = TestRun(
        name="TTL Timeout",
        created_by="qa",
        state=RunState.active,
        requirements=[],
        created_at=datetime.now(UTC) - timedelta(minutes=31),
        ttl_minutes=30,
        heartbeat_timeout_sec=600,
    )
    db_session.add(stale_run)
    await db_session.commit()

    with patch("app.runs.service_reaper.run_service.expire_run", new_callable=AsyncMock) as expire_run:
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

    with patch("app.runs.service_reaper.run_service.expire_run", new_callable=AsyncMock) as expire_run:
        await _reap_stale_runs(db_session)

    expire_run.assert_not_awaited()


async def test_expire_run_deletes_active_grid_session(
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.devices.models import DeviceOperationalState
    from app.runs import service as run_service
    from app.sessions.models import Session, SessionStatus
    from tests.helpers import create_device_record, create_reserved_run

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="expire-live-session",
        connection_target="expire-live-session",
        name="Expire Live Session",
        operational_state=DeviceOperationalState.busy,
    )
    run = await create_reserved_run(
        db_session,
        name="Expire Live Session Run",
        devices=[device],
        state=RunState.active,
    )
    db_session.add(
        Session(
            session_id="grid-live-expire",
            device_id=device.id,
            run_id=run.id,
            test_name="test_expire_cleanup",
            status=SessionStatus.running,
        )
    )
    await db_session.commit()

    deleted: list[str] = []

    async def fake_terminate(session_id: str) -> bool:
        deleted.append(session_id)
        return True

    monkeypatch.setattr(run_lifecycle_release.grid_service, "terminate_grid_session", fake_terminate)

    await run_service.expire_run(db_session, run, "Heartbeat timeout")

    assert deleted == ["grid-live-expire"]
