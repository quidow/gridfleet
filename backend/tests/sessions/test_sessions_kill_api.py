from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.jobs import JOB_STATUS_PENDING
from app.jobs.kinds import JOB_KIND_SESSION_KILL
from app.jobs.models import Job
from app.sessions import service_kill
from app.sessions.models import Session, SessionStatus
from app.sessions.service_kill import SessionKillService
from tests.helpers import create_device_record
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.devices.models import Device


class _TxTracker:
    def __init__(self) -> None:
        self.active = 0


class _TrackingCtx(AbstractAsyncContextManager["AsyncSession"]):
    def __init__(self, inner: AbstractAsyncContextManager[AsyncSession], tracker: _TxTracker) -> None:
        self._inner = inner
        self._tracker = tracker

    async def __aenter__(self) -> AsyncSession:
        db = await self._inner.__aenter__()
        self._tracker.active += 1
        return db

    async def __aexit__(self, *exc: object) -> bool | None:
        try:
            return await self._inner.__aexit__(*exc)
        finally:
            self._tracker.active -= 1


class _TrackingFactory:
    def __init__(self, inner: async_sessionmaker[AsyncSession], tracker: _TxTracker) -> None:
        self._inner = inner
        self._tracker = tracker

    def __call__(self) -> _TrackingCtx:
        return _TrackingCtx(self._inner(), self._tracker)

    def begin(self) -> _TrackingCtx:
        return _TrackingCtx(self._inner.begin(), self._tracker)


async def _device(db_session: AsyncSession, host_id: str) -> Device:
    return await create_device_record(
        db_session,
        host_id=host_id,
        identity_value="kill-test-device",
        connection_target="kill-test-device",
        name="Kill Test Phone",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        os_version="14",
    )


@pytest.fixture
def terminate_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def fake_terminate(target: str, session_id: str, *, timeout: float = 10.0) -> bool:
        calls.append((target, session_id))
        return True

    monkeypatch.setattr(service_kill.appium_direct, "terminate_session", fake_terminate)
    return calls


async def test_kill_running_session(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str, terminate_calls: list[tuple[str, str]]
) -> None:
    from app.sessions.models import Session, SessionStatus

    device = await _device(db_session, default_host_id)
    session = Session(
        session_id="kill-me-1",
        device_id=device.id,
        status=SessionStatus.running,
        router_target="http://10.0.0.5:4723",
    )
    db_session.add(session)
    await db_session.commit()
    db_session.expunge(session)

    resp = await client.post("/api/sessions/kill-me-1/kill")
    assert resp.status_code == 200
    data = resp.json()
    assert data["terminated"] is True
    assert data["session"]["status"] == "error"
    assert data["session"]["error_type"] == "operator_kill"
    assert data["session"]["ended_at"] is not None
    # No live appium_node in this test, so the stored router_target is used.
    assert terminate_calls == [("http://10.0.0.5:4723", "kill-me-1")]


async def test_kill_terminalizes_even_when_appium_unreachable(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.sessions.models import Session, SessionStatus

    async def failing_terminate(target: str, session_id: str, *, timeout: float = 10.0) -> bool:
        return False

    monkeypatch.setattr(service_kill.appium_direct, "terminate_session", failing_terminate)

    device = await _device(db_session, default_host_id)
    session = Session(
        session_id="kill-me-2",
        device_id=device.id,
        status=SessionStatus.running,
        router_target="http://10.0.0.5:4723",
    )
    db_session.add(session)
    await db_session.commit()
    db_session.expunge(session)

    resp = await client.post("/api/sessions/kill-me-2/kill")
    assert resp.status_code == 200
    data = resp.json()
    assert data["terminated"] is False
    assert data["session"]["status"] == "error"
    assert data["session"]["ended_at"] is not None


async def test_kill_session_without_target_skips_terminate(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str, terminate_calls: list[tuple[str, str]]
) -> None:
    from app.sessions.models import Session, SessionStatus

    device = await _device(db_session, default_host_id)
    session = Session(session_id="kill-me-3", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()
    db_session.expunge(session)

    resp = await client.post("/api/sessions/kill-me-3/kill")
    assert resp.status_code == 200
    assert resp.json()["terminated"] is False
    assert terminate_calls == []


async def test_kill_unknown_session_is_404(client: AsyncClient, terminate_calls: list[tuple[str, str]]) -> None:
    resp = await client.post("/api/sessions/nope/kill")
    assert resp.status_code == 404
    assert terminate_calls == []


async def test_kill_ended_session_is_409(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str, terminate_calls: list[tuple[str, str]]
) -> None:
    from app.sessions.models import Session, SessionStatus

    device = await _device(db_session, default_host_id)
    session = Session(
        session_id="already-done",
        device_id=device.id,
        status=SessionStatus.passed,
        ended_at=datetime.now(UTC),
    )
    db_session.add(session)
    await db_session.commit()
    db_session.expunge(session)

    resp = await client.post("/api/sessions/already-done/kill")
    assert resp.status_code == 409
    assert terminate_calls == []


async def test_kill_pending_session_is_409(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str, terminate_calls: list[tuple[str, str]]
) -> None:
    """Pending rows (allocate->confirm window) have no Appium session yet; the
    allocation reaper owns them. Kill applies to running sessions only."""
    from app.sessions.models import Session, SessionStatus

    device = await _device(db_session, default_host_id)
    session = Session(session_id="alloc-pending", device_id=device.id, status=SessionStatus.pending)
    db_session.add(session)
    await db_session.commit()
    db_session.expunge(session)

    resp = await client.post("/api/sessions/alloc-pending/kill")
    assert resp.status_code == 409
    assert terminate_calls == []


async def _running_session(db_session: AsyncSession, host_id: str, session_id: str) -> Session:
    device = await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=f"kill-{session_id}",
        connection_target=f"kill-{session_id}",
        name=f"Kill {session_id}",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        os_version="14",
    )
    session = Session(
        session_id=session_id,
        device_id=device.id,
        status=SessionStatus.running,
        router_target="http://10.0.0.5:4723",
    )
    db_session.add(session)
    await db_session.commit()
    return session


def _kill_service(session_factory: object) -> SessionKillService:
    return SessionKillService(publisher=event_bus, session_factory=session_factory)  # type: ignore[arg-type]


async def test_kill_crash_after_prepare_recovers_via_worker(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_kill.appium_direct, "terminate_session", AsyncMock(return_value=True))
    session = await _running_session(db_session, default_host_id, "kill-crash")

    svc = _kill_service(db_session_maker)
    effect = await svc.prepare("kill-crash")
    assert effect is not None

    job = (await db_session.execute(select(Job).where(Job.kind == JOB_KIND_SESSION_KILL))).scalar_one()
    assert job.status == JOB_STATUS_PENDING
    await db_session.refresh(session)
    assert session.status == SessionStatus.running
    assert session.ended_at is None

    await svc.run_session_kill_job(str(job.id), job.payload)

    await db_session.refresh(session)
    assert session.status == SessionStatus.error
    assert session.error_type == "operator_kill"
    assert session.ended_at is not None


async def test_kill_concurrent_natural_end_wins(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_kill.appium_direct, "terminate_session", AsyncMock(return_value=True))
    session = await _running_session(db_session, default_host_id, "kill-natural")

    svc = _kill_service(db_session_maker)
    effect = await svc.prepare("kill-natural")
    assert effect is not None

    # A concurrent natural end terminalizes the row before finalize runs.
    async with db_session_maker() as side:
        row = await side.get(Session, session.id)
        assert row is not None
        row.status = SessionStatus.passed
        row.ended_at = datetime.now(UTC)
        await side.commit()

    terminated = await service_kill._perform_kill_effect(effect)
    outcome = await svc.finalize(effect, terminated)

    # The natural end wins: status stays passed, no operator_kill overwrite.
    assert outcome.session.status == SessionStatus.passed
    await db_session.refresh(session)
    assert session.status == SessionStatus.passed


async def test_kill_same_job_repeated_delete_terminalizes_once(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deletes: list[str] = []

    async def fake_terminate(target: str, session_id: str, *, timeout: float = 10.0) -> bool:
        deletes.append(session_id)
        return True

    monkeypatch.setattr(service_kill.appium_direct, "terminate_session", fake_terminate)
    session = await _running_session(db_session, default_host_id, "kill-repeat")

    svc = _kill_service(db_session_maker)
    effect = await svc.prepare("kill-repeat")
    assert effect is not None

    # The effect may be replayed for the same operation id: DELETE repeats.
    assert await service_kill._perform_kill_effect(effect) is True
    assert await service_kill._perform_kill_effect(effect) is True
    assert deletes == ["kill-repeat", "kill-repeat"]

    outcome = await svc.finalize(effect, True)
    assert outcome.terminated is True
    await db_session.refresh(session)
    assert session.status == SessionStatus.error
    assert session.ended_at is not None


async def test_kill_delete_sees_no_active_transaction(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _TxTracker()
    calls: list[str] = []

    async def fake_terminate(target: str, session_id: str, *, timeout: float = 10.0) -> bool:
        assert tracker.active == 0, "Appium DELETE issued with an open transaction"
        calls.append(session_id)
        return True

    monkeypatch.setattr(service_kill.appium_direct, "terminate_session", fake_terminate)
    await _running_session(db_session, default_host_id, "kill-notx")

    svc = _kill_service(_TrackingFactory(db_session_maker, tracker))
    outcome = await svc.kill("kill-notx")

    assert outcome is not None
    assert outcome.terminated is True
    assert calls == ["kill-notx"]
