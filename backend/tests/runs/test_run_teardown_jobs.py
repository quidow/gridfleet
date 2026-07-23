"""Durable run-session teardown: crash / retry / stale / no-transaction proofs."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import (
    ConnectionType,
    Device,
    DeviceOperationalState,
    DeviceReservation,
    DeviceType,
)
from app.jobs import JOB_STATUS_FAILED, JOB_STATUS_PENDING
from app.jobs.kinds import JOB_KIND_RUN_SESSION_TEARDOWN
from app.jobs.models import Job
from app.runs.models import RunState, TestRun
from app.runs.service_lifecycle_release import RunReleaseService
from app.runs.service_teardown import (
    RunTeardownKind,
    RunTeardownResult,
    RunTeardownService,
    perform_run_teardown_effect,
)
from app.sessions.models import Session, SessionStatus
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

pytestmark = pytest.mark.asyncio

_settings = FakeSettingsReader({})


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


def _service(session_factory: object) -> RunTeardownService:
    release = RunReleaseService(publisher=event_bus, settings=_settings, deferred_stop=AsyncMock())
    return RunTeardownService(
        publisher=event_bus,
        settings=_settings,
        release=release,
        session_factory=session_factory,  # type: ignore[arg-type]
    )


async def _seed(db_session: AsyncSession, host_id: object, suffix: str) -> tuple[Device, TestRun, Session]:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=f"td-{suffix}",
        connection_target=f"td-{suffix}",
        name=f"Teardown {suffix}",
        os_version="14",
        host_id=host_id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        verified_at=datetime.now(UTC),
    )
    db_session.add(device)
    run = TestRun(
        id=uuid4(),
        name=f"td-run-{suffix}",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        DeviceReservation(
            run_id=run.id,
            device_id=device.id,
            identity_value=device.identity_value,
            connection_target=device.connection_target,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            os_version=device.os_version,
        )
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=1,
            active_connection_target="http://10.0.0.1:4723",
            health_running=True,
            health_state="ready",
        )
    )
    session = Session(
        session_id=f"sess-td-{suffix}",
        device_id=device.id,
        run_id=run.id,
        test_name="test_teardown",
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()
    return device, run, session


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []

    def capture(
        _self: object, _db: object, name: str, payload: dict[str, object], *, severity: str | None = None
    ) -> None:
        events.append((name, payload))

    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", capture)
    return events


@pytest.mark.db
async def test_crash_after_prepare_leaves_pending_recoverable_job(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.runs.service_teardown.appium_direct.terminate_session", AsyncMock(return_value=True))
    monkeypatch.setattr("app.runs.service_teardown.appium_direct.session_alive", AsyncMock(return_value=False))
    device, run, session = await _seed(db_session, db_host.id, "crash")

    svc = _service(db_session_maker)
    effect = await svc.prepare(RunTeardownKind.cancel, run.id, None)
    assert effect is not None

    # Crash simulated: prepare committed a pending job, but did not terminalize.
    job = (await db_session.execute(select(Job).where(Job.kind == JOB_KIND_RUN_SESSION_TEARDOWN))).scalar_one()
    assert job.status == JOB_STATUS_PENDING
    await db_session.refresh(run)
    await db_session.refresh(device)
    await db_session.refresh(session)
    assert run.state == RunState.active
    assert device.operational_state_last_emitted == DeviceOperationalState.busy
    assert session.ended_at is None
    reservation = (
        await db_session.execute(select(DeviceReservation).where(DeviceReservation.run_id == run.id))
    ).scalar_one()
    assert reservation.released_at is None

    # Durable worker recovery replays effect + finalize idempotently.
    await svc.run_run_session_teardown_job(str(job.id), job.payload)

    await db_session.refresh(run)
    await db_session.refresh(session)
    await db_session.refresh(reservation)
    assert run.state == RunState.cancelled
    assert session.status == SessionStatus.error
    assert session.ended_at is not None
    assert reservation.released_at is not None


@pytest.mark.db
async def test_retry_after_delete_reuses_operation_id_and_terminalizes_once(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[tuple[str, dict[str, object]]],
) -> None:
    deletes: list[str] = []

    async def fake_terminate(target: str, session_id: str, *, timeout: float = 10.0) -> bool:
        deletes.append(session_id)
        return True

    monkeypatch.setattr("app.runs.service_teardown.appium_direct.terminate_session", fake_terminate)
    _device, run, _session = await _seed(db_session, db_host.id, "retry")

    svc = _service(db_session_maker)
    effect = await svc.prepare(RunTeardownKind.cancel, run.id, None)
    assert effect is not None

    # The effect may be replayed (crash-retry): Appium DELETE repeats harmlessly.
    result_a = await perform_run_teardown_effect(effect)
    result_b = await perform_run_teardown_effect(effect)
    assert result_a.terminated_session_ids == result_b.terminated_session_ids
    assert len(deletes) == 2  # repeated DELETE allowed

    await svc.finalize(effect, result_b)

    assert [name for name, _ in captured_events].count("run.cancelled") == 1
    assert [name for name, _ in captured_events].count("session.ended") == 1
    reservations = (
        (await db_session.execute(select(DeviceReservation).where(DeviceReservation.run_id == run.id))).scalars().all()
    )
    assert len(reservations) == 1
    await db_session.refresh(reservations[0])
    assert reservations[0].released_at is not None


@pytest.mark.db
async def test_stale_finalize_after_newer_operation_is_noop(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.runs.service_teardown.appium_direct.terminate_session", AsyncMock(return_value=True))
    device, run, session = await _seed(db_session, db_host.id, "stale")

    svc = _service(db_session_maker)
    effect = await svc.prepare(RunTeardownKind.cancel, run.id, None)
    assert effect is not None

    # The old operation is superseded: fail its job, then a newer operation is prepared.
    async with db_session_maker() as side:
        job = await side.get(Job, effect.operation_id)
        assert job is not None
        job.status = JOB_STATUS_FAILED
        await side.commit()
    newer = await svc.prepare(RunTeardownKind.cancel, run.id, None)
    assert newer is not None
    assert newer.operation_id != effect.operation_id

    # The stale finalizer changes no run/device/session/reservation fact.
    await svc.finalize(effect, RunTeardownResult(frozenset({session.id}), frozenset()))

    await db_session.refresh(run)
    await db_session.refresh(device)
    await db_session.refresh(session)
    assert run.state == RunState.active
    assert device.operational_state_last_emitted == DeviceOperationalState.busy
    assert session.ended_at is None
    reservation = (
        await db_session.execute(select(DeviceReservation).where(DeviceReservation.run_id == run.id))
    ).scalar_one()
    assert reservation.released_at is None


@pytest.mark.db
async def test_appium_delete_and_survivor_probe_run_after_run_and_device_locks_release(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _device, run, _session = await _seed(db_session, db_host.id, "notx")
    tracker = _TxTracker()

    terminated: list[str] = []
    probed: list[str] = []

    async def fake_terminate(target: str, session_id: str, *, timeout: float = 10.0) -> bool:
        assert tracker.active == 0, "Appium DELETE issued with an open transaction"
        terminated.append(session_id)
        return True

    async def fake_alive(target: str, session_id: str, *, timeout: float = 10.0) -> bool | None:
        assert tracker.active == 0, "survivor probe issued with an open transaction"
        probed.append(session_id)
        return False

    monkeypatch.setattr("app.runs.service_teardown.appium_direct.terminate_session", fake_terminate)
    monkeypatch.setattr("app.runs.service_teardown.appium_direct.session_alive", fake_alive)

    svc = _service(_TrackingFactory(db_session_maker, tracker))
    await svc.teardown_run(RunTeardownKind.force_release, run.id)

    assert terminated == ["sess-td-notx"]
    assert probed == ["sess-td-notx"]
    await db_session.refresh(run)
    assert run.state == RunState.cancelled
