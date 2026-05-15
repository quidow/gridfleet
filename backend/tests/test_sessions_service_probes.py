import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.hosts.models import Host
from app.sessions import service as session_service
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME


async def _seed(db_session: AsyncSession, db_host: Host, suffix: str) -> tuple[Session, Session]:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=f"probe-svc-{suffix}",
        connection_target=f"probe-svc-{suffix}",
        name=f"Probe Svc {suffix}",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    real = Session(
        id=uuid.uuid4(),
        session_id=f"real-{suffix}",
        device_id=device.id,
        test_name="test_login",
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        status=SessionStatus.passed,
    )
    probe = Session(
        id=uuid.uuid4(),
        session_id=f"probe-{suffix}",
        device_id=device.id,
        test_name=PROBE_TEST_NAME,
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        status=SessionStatus.passed,
    )
    db_session.add_all([real, probe])
    await db_session.flush()
    return real, probe


@pytest.mark.db
async def test_list_sessions_hides_probes_by_default(db_session: AsyncSession, db_host: Host) -> None:
    real, _ = await _seed(db_session, db_host, "default")
    sessions, _total = await session_service.list_sessions(db_session)
    ids = {s.id for s in sessions}
    assert real.id in ids
    assert all(s.test_name != PROBE_TEST_NAME for s in sessions)


@pytest.mark.db
async def test_list_sessions_includes_probes_when_requested(db_session: AsyncSession, db_host: Host) -> None:
    real, probe = await _seed(db_session, db_host, "include")
    sessions, _total = await session_service.list_sessions(db_session, include_probes=True)
    ids = {s.id for s in sessions}
    assert real.id in ids
    assert probe.id in ids


@pytest.mark.db
async def test_list_sessions_cursor_hides_probes_by_default(db_session: AsyncSession, db_host: Host) -> None:
    real, _ = await _seed(db_session, db_host, "cursor-default")
    page = await session_service.list_sessions_cursor(db_session)
    ids = {s.id for s in page.items}
    assert real.id in ids
    assert all(s.test_name != PROBE_TEST_NAME for s in page.items)


@pytest.mark.db
async def test_list_sessions_cursor_includes_probes_when_requested(db_session: AsyncSession, db_host: Host) -> None:
    real, probe = await _seed(db_session, db_host, "cursor-include")
    page = await session_service.list_sessions_cursor(db_session, include_probes=True)
    ids = {s.id for s in page.items}
    assert real.id in ids
    assert probe.id in ids


@pytest.mark.db
async def test_get_device_sessions_hides_probes_by_default(db_session: AsyncSession, db_host: Host) -> None:
    real, _ = await _seed(db_session, db_host, "dev-default")
    assert real.device_id is not None
    sessions = await session_service.get_device_sessions(db_session, real.device_id)
    ids = {s.id for s in sessions}
    assert real.id in ids
    assert all(s.test_name != PROBE_TEST_NAME for s in sessions)


@pytest.mark.db
async def test_get_device_sessions_includes_probes_when_requested(db_session: AsyncSession, db_host: Host) -> None:
    real, probe = await _seed(db_session, db_host, "dev-include")
    assert real.device_id is not None
    sessions = await session_service.get_device_sessions(db_session, real.device_id, include_probes=True)
    ids = {s.id for s in sessions}
    assert real.id in ids
    assert probe.id in ids
