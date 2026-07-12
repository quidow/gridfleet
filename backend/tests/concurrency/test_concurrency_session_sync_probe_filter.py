# backend/tests/test_concurrency_session_sync_probe_filter.py
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.sessions import service_sync
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME
from app.sessions.service_sync import SessionSyncService
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.asyncio


async def _seed(db: AsyncSession, host: Host, identity: str) -> Device:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=identity,
        connection_target=identity,
        name=identity,
        os_version="14",
        host_id=host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db.add(device)
    await db.flush()
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=42,
        active_connection_target=device.connection_target,
    )
    db.add(node)
    await db.commit()
    return device


async def test_probe_create_window_spared_by_pending_row(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """During the create window the probe's real Appium id is not yet known: the
    pending birth row makes the orphan sweep spare unknown ids on the device — the
    same rule that protects backend-owned client creates."""
    device = await _seed(db_session, db_host, "probe-filter")
    db_session.add(
        Session(
            session_id=f"probe-{uuid.uuid4()}",
            device_id=device.id,
            test_name=PROBE_TEST_NAME,
            status=SessionStatus.pending,
        )
    )
    await db_session.commit()
    terminated: list[Any] = []

    async def fake_list(_target: str, **_: object) -> list[str]:
        return ["probe-live-uuid"]

    async def fake_terminate(t: str, sid: str, **_: object) -> bool:
        terminated.append((t, sid))
        return True

    monkeypatch.setattr(service_sync.appium_direct, "list_sessions", fake_list)
    monkeypatch.setattr(service_sync.appium_direct, "terminate_session", fake_terminate)

    svc = SessionSyncService(publisher=event_bus, settings=FakeSettingsReader({}), lifecycle=AsyncMock())
    await svc.sync(db_session)

    assert terminated == []


async def test_running_probe_row_matches_like_any_session(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After promotion the row carries the real Appium id: the sweep matches it
    as a known session — no sparing branch, no shadow bookkeeping."""
    device = await _seed(db_session, db_host, "probe-promoted")
    db_session.add(
        Session(
            session_id="probe-live-uuid",
            device_id=device.id,
            test_name=PROBE_TEST_NAME,
            status=SessionStatus.running,
        )
    )
    await db_session.commit()
    terminated: list[Any] = []

    async def fake_list(_target: str, **_: object) -> list[str]:
        return ["probe-live-uuid"]

    async def fake_terminate(t: str, sid: str, **_: object) -> bool:
        terminated.append((t, sid))
        return True

    async def fake_alive(_target: str, _sid: str, **_: object) -> bool:
        return True

    monkeypatch.setattr(service_sync.appium_direct, "list_sessions", fake_list)
    monkeypatch.setattr(service_sync.appium_direct, "terminate_session", fake_terminate)
    monkeypatch.setattr(service_sync.appium_direct, "session_alive", fake_alive)

    svc = SessionSyncService(publisher=event_bus, settings=FakeSettingsReader({}), lifecycle=AsyncMock())
    await svc.sync(db_session)

    assert terminated == []


async def test_crash_orphaned_probe_row_reaped_and_appium_session_killed(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-probe scheduler kill (after promotion, before terminate) leaves an
    ordinary crash-orphaned running row: the never-commanded reap closes it and
    terminates its Appium session — existing machinery, no probe TTL (WS-16.1).
    The close is event-silent (probes never emitted session.started)."""
    device = await _seed(db_session, db_host, "probe-crash")
    row = Session(
        session_id="probe-crash-appium-id",
        device_id=device.id,
        test_name=PROBE_TEST_NAME,
        status=SessionStatus.running,
        started_at=datetime.now(UTC) - timedelta(hours=1),
        router_target=f"http://{db_host.ip}:4723",
    )
    db_session.add(row)
    await db_session.commit()
    terminated: list[Any] = []
    events: list[str] = []

    async def fake_terminate(t: str, sid: str, **_: object) -> bool:
        terminated.append((t, sid))
        return True

    async def fake_list(_target: str, **_: object) -> list[str]:
        return []

    monkeypatch.setattr(service_sync.appium_direct, "terminate_session", fake_terminate)
    monkeypatch.setattr(service_sync.appium_direct, "list_sessions", fake_list)

    class _Publisher:
        def queue_for_session(self, _db: object, event_type: str, _data: object, **_: object) -> None:
            events.append(event_type)

        async def publish(self, event_type: str, _data: object, **_: object) -> None:
            events.append(event_type)

        def track_task(self, _task: object) -> None:
            pass

    svc = SessionSyncService(publisher=_Publisher(), settings=FakeSettingsReader({}), lifecycle=AsyncMock())
    await svc.sync(db_session)
    await db_session.refresh(row)

    assert [sid for _target, sid in terminated] == ["probe-crash-appium-id"]
    assert row.ended_at is not None
    assert [event for event in events if event.startswith("session.")] == []


async def test_orphan_session_killed_when_no_probe(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(db_session, db_host, "real-session")
    target = f"http://{db_host.ip}:4723"
    terminated: list[Any] = []

    async def fake_list(_target: str, **_: object) -> list[str]:
        return ["orphan-uuid"]

    async def fake_terminate(t: str, sid: str, **_: object) -> bool:
        terminated.append((t, sid))
        return True

    monkeypatch.setattr(service_sync.appium_direct, "list_sessions", fake_list)
    monkeypatch.setattr(service_sync.appium_direct, "terminate_session", fake_terminate)

    svc = SessionSyncService(publisher=event_bus, settings=FakeSettingsReader({}), lifecycle=AsyncMock())
    await svc.sync(db_session)

    assert terminated == [(target, "orphan-uuid")]
