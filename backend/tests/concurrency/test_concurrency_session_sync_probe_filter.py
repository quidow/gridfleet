# backend/tests/test_concurrency_session_sync_probe_filter.py
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services import state_write_guard
from app.sessions import probe_inflight, service_sync
from app.sessions.service_sync import SessionSyncService
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _skip_leader_fencing() -> Iterator[None]:
    with patch("app.sessions.service_sync.assert_current_leader"):
        yield


async def _seed(db: AsyncSession, host: Host, identity: str) -> Device:
    with state_write_guard.bypass():
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
    with state_write_guard.bypass():
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


async def test_inflight_probe_session_not_killed(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A viability probe's live Appium session must not be terminated as an orphan."""
    device = await _seed(db_session, db_host, "probe-filter")
    terminated: list[Any] = []

    async def fake_list(_target: str, **_: object) -> list[str]:
        return ["probe-live-uuid"]

    async def fake_terminate(t: str, sid: str, **_: object) -> bool:
        terminated.append((t, sid))
        return True

    monkeypatch.setattr(service_sync.appium_direct, "list_sessions", fake_list)
    monkeypatch.setattr(service_sync.appium_direct, "terminate_session", fake_terminate)

    svc = SessionSyncService(publisher=event_bus, settings=FakeSettingsReader({}), lifecycle=AsyncMock())
    probe_inflight.mark_probe_started(str(device.id))
    try:
        await svc.sync(db_session)
    finally:
        probe_inflight.mark_probe_finished(str(device.id))

    assert terminated == []


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
