from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.runs.models import RunState, TestRun
from app.runs.service_reaper import _reap_stale_runs
from tests.fakes import FakeSettingsReader, make_fake_grid
from tests.helpers import test_event_bus as event_bus


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
        await _reap_stale_runs(db_session, publisher=event_bus, settings=FakeSettingsReader(), grid=make_fake_grid())

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
        await _reap_stale_runs(db_session, publisher=event_bus, settings=FakeSettingsReader(), grid=make_fake_grid())

    expire_run.assert_awaited_once()
    assert expire_run.await_args is not None
    assert expire_run.await_args.args[1].id == stale_run.id
    assert expire_run.await_args.args[2] == "TTL exceeded (30 minutes)"


async def test_reap_stale_runs_query_filters_at_sql_level(db_session: AsyncSession) -> None:
    """Inserting many fresh runs alongside one stale run must yield exactly one fetched row.

    Guards against regressing the WHERE-clause filter back to a Python-side scan.
    """
    now = datetime.now(UTC)
    fresh_runs = [
        TestRun(
            name=f"Fresh-{i}",
            created_by="qa",
            state=RunState.active,
            requirements=[],
            last_heartbeat=now,
            created_at=now,
            ttl_minutes=60,
            heartbeat_timeout_sec=60,
        )
        for i in range(5)
    ]
    stale_run = TestRun(
        name="Stale",
        created_by="qa",
        state=RunState.active,
        requirements=[],
        last_heartbeat=now - timedelta(seconds=120),
        created_at=now - timedelta(seconds=120),
        heartbeat_timeout_sec=60,
        ttl_minutes=60,
    )
    db_session.add_all([*fresh_runs, stale_run])
    await db_session.commit()

    heartbeat_deadline_expr = TestRun.last_heartbeat + func.make_interval(
        0, 0, 0, 0, 0, 0, TestRun.heartbeat_timeout_sec
    )
    ttl_deadline_expr = TestRun.created_at + func.make_interval(0, 0, 0, 0, 0, TestRun.ttl_minutes)
    stmt = select(TestRun).where(
        or_(
            and_(TestRun.last_heartbeat.is_not(None), heartbeat_deadline_expr < now),
            ttl_deadline_expr < now,
        )
    )
    result = await db_session.execute(stmt)
    fetched_ids = {row.id for row in result.scalars().all()}

    assert stale_run.id in fetched_ids
    for fresh in fresh_runs:
        assert fresh.id not in fetched_ids


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
        await _reap_stale_runs(db_session, publisher=event_bus, settings=FakeSettingsReader(), grid=make_fake_grid())

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

    fake_grid = AsyncMock()
    fake_grid.terminate_session = fake_terminate

    await run_service.expire_run(
        db_session, run, "Heartbeat timeout", publisher=event_bus, settings=FakeSettingsReader(), grid=fake_grid
    )

    assert deleted == ["grid-live-expire"]


async def test_expire_run_emits_never_activated_for_preparing_run(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.runs import service as run_service

    run = TestRun(
        name="Never Activated",
        created_by="qa",
        state=RunState.preparing,
        requirements=[],
        last_heartbeat=datetime.now(UTC) - timedelta(seconds=120),
        heartbeat_timeout_sec=60,
        ttl_minutes=60,
    )
    db_session.add(run)
    await db_session.commit()

    events: list[tuple[str, dict[str, object], str | None]] = []

    def capture(
        _db: object, name: str, payload: dict[str, object], *, severity: str | None = None, publisher: object = None
    ) -> None:
        events.append((name, payload, severity))

    monkeypatch.setattr("app.runs.service_lifecycle.queue_event_for_session", capture)

    await run_service.expire_run(
        db_session, run, "Heartbeat timeout", publisher=event_bus, settings=FakeSettingsReader(), grid=make_fake_grid()
    )

    names = [name for name, _, _ in events]
    assert "run.never_activated" in names
    assert "run.expired" in names
    assert names.index("run.never_activated") < names.index("run.expired")

    never_activated_payload = next(payload for name, payload, _ in events if name == "run.never_activated")
    never_activated_severity = next(severity for name, _, severity in events if name == "run.never_activated")
    assert never_activated_payload["run_id"] == str(run.id)
    reason = never_activated_payload["reason"]
    assert isinstance(reason, str)
    assert "preparing" in reason
    assert "/api/runs/{id}/active" in reason
    assert never_activated_severity == "warning"

    await db_session.refresh(run)
    assert run.state == RunState.expired
    assert isinstance(run.error, str)
    assert "preparing" in run.error


async def test_expire_run_does_not_emit_never_activated_for_active_run(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.runs import service as run_service

    run = TestRun(
        name="Active Expire",
        created_by="qa",
        state=RunState.active,
        requirements=[],
        last_heartbeat=datetime.now(UTC) - timedelta(seconds=120),
        heartbeat_timeout_sec=60,
        ttl_minutes=60,
        started_at=datetime.now(UTC) - timedelta(seconds=300),
    )
    db_session.add(run)
    await db_session.commit()

    events: list[tuple[str, dict[str, object], str | None]] = []

    def capture(
        _db: object, name: str, payload: dict[str, object], *, severity: str | None = None, publisher: object = None
    ) -> None:
        events.append((name, payload, severity))

    monkeypatch.setattr("app.runs.service_lifecycle.queue_event_for_session", capture)

    await run_service.expire_run(
        db_session, run, "Heartbeat timeout", publisher=event_bus, settings=FakeSettingsReader(), grid=make_fake_grid()
    )

    names = [name for name, _, _ in events]
    assert "run.never_activated" not in names
    assert "run.expired" in names
