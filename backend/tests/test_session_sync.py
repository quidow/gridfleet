from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import ConnectionType, Device, DeviceAvailabilityStatus, DeviceType
from app.models.host import Host
from app.models.session import Session, SessionStatus
from app.models.test_run import RunState, TestRun
from app.services.lifecycle_policy import handle_health_failure
from app.services.session_sync import _sync_sessions

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _grid_response(sessions_per_node: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build a mock Grid /status response.

    Grid 4 stores sessions under node.slots[].session (not node.sessions).
    """
    if sessions_per_node is None:
        sessions_per_node = []
    slots: list[dict[str, Any]] = []
    for sess in sessions_per_node:
        slots.append({"session": sess})
    # Pad with empty slots so the node has slots even with no active sessions
    if not slots:
        slots.append({"session": None})
    return {
        "value": {
            "ready": True,
            "message": "Selenium Grid ready.",
            "nodes": [
                {
                    "id": "node-1",
                    "slots": slots,
                    "availability": "UP",
                }
            ],
        }
    }


def _grid_session(
    session_id: str,
    connection_target: str,
    test_name: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    """Build a single slot session entry for the Grid response."""
    caps = {"platformName": "android", "appium:udid": connection_target}
    if test_name:
        caps["gridfleet:testName"] = test_name
    if device_id:
        caps["appium:gridfleet:deviceId"] = device_id
    return {"sessionId": session_id, "capabilities": caps}


async def test_sync_creates_session(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-001",
        connection_target="dev-001",
        name="Test Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    grid_data = _grid_response([_grid_session("sess-1", "dev-001", "test_login")])

    with patch("app.services.session_sync.grid_service.get_grid_status", return_value=grid_data):
        await _sync_sessions(db_session)

    result = await db_session.execute(select(Session).where(Session.session_id == "sess-1"))
    session = result.scalar_one()
    assert session.status == SessionStatus.running
    assert session.test_name == "test_login"
    assert session.device_id == device.id

    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.busy


async def test_sync_ends_session(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-002",
        connection_target="dev-002",
        name="Test Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.busy,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    session = Session(session_id="sess-2", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    # Grid reports no sessions
    grid_data = _grid_response([])

    with patch("app.services.session_sync.grid_service.get_grid_status", return_value=grid_data):
        await _sync_sessions(db_session)

    await db_session.refresh(session)
    assert session.status == SessionStatus.passed
    assert session.ended_at is not None

    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.available


async def test_sync_ends_session_after_identity_map_reset(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-002b",
        connection_target="dev-002b",
        name="Reset Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.busy,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    session = Session(session_id="sess-2b", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()
    db_session.expunge_all()

    with patch("app.services.session_sync.grid_service.get_grid_status", return_value=_grid_response([])):
        await _sync_sessions(db_session)

    result = await db_session.execute(select(Session).where(Session.session_id == "sess-2b"))
    ended_session = result.scalar_one()
    assert ended_session.status == SessionStatus.passed
    assert ended_session.ended_at is not None

    refreshed_device = await db_session.get(Device, device.id)
    assert refreshed_device is not None
    assert refreshed_device.availability_status == DeviceAvailabilityStatus.available


async def test_sync_ignores_unknown_connection_target(db_session: AsyncSession) -> None:
    grid_data = _grid_response([_grid_session("sess-3", "unknown-device")])

    with patch("app.services.session_sync.grid_service.get_grid_status", return_value=grid_data):
        await _sync_sessions(db_session)

    result = await db_session.execute(select(Session))
    assert result.scalars().all() == []


async def test_sync_uses_manager_device_id_when_udid_is_transient(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="manager_generated",
        identity_scope="host",
        identity_value="avd:Pixel_6_API_35",
        connection_target="Pixel_6_API_35",
        name="Pixel 6",
        os_version="15",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    grid_data = _grid_response([_grid_session("sess-avd", "emulator-5554", "test_login", device_id=str(device.id))])

    with patch("app.services.session_sync.grid_service.get_grid_status", return_value=grid_data):
        await _sync_sessions(db_session)

    result = await db_session.execute(select(Session).where(Session.session_id == "sess-avd"))
    session = result.scalar_one()
    assert session.device_id == device.id

    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.busy


async def test_sync_preserves_busy_for_multi_session(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-004",
        connection_target="dev-004",
        name="Multi Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.busy,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    s1 = Session(session_id="sess-4a", device_id=device.id, status=SessionStatus.running)
    s2 = Session(session_id="sess-4b", device_id=device.id, status=SessionStatus.running)
    db_session.add_all([s1, s2])
    await db_session.commit()

    # Only sess-4b is still running on Grid
    grid_data = _grid_response([_grid_session("sess-4b", "dev-004")])

    with patch("app.services.session_sync.grid_service.get_grid_status", return_value=grid_data):
        await _sync_sessions(db_session)

    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.busy  # sess-4b still running


async def test_sync_startup_recovery(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-005",
        connection_target="dev-005",
        name="Recovery Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.busy,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    session = Session(session_id="sess-5", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    # Grid still has the session running
    grid_data = _grid_response([_grid_session("sess-5", "dev-005")])

    with patch("app.services.session_sync.grid_service.get_grid_status", return_value=grid_data):
        await _sync_sessions(db_session)

    result = await db_session.execute(select(Session).where(Session.session_id == "sess-5"))
    sessions = result.scalars().all()
    assert len(sessions) == 1  # not duplicated


async def test_sync_does_not_duplicate_terminal_session_seen_active_again(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-terminal-race",
        connection_target="dev-terminal-race",
        name="Terminal Race Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.busy,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    session = Session(
        session_id="sess-terminal-race",
        device_id=device.id,
        test_name="test_terminal_race",
        status=SessionStatus.passed,
        ended_at=datetime.now(UTC),
    )
    db_session.add(session)
    await db_session.commit()

    grid_data = _grid_response([_grid_session("sess-terminal-race", "dev-terminal-race", "test_terminal_race")])

    with patch("app.services.session_sync.grid_service.get_grid_status", return_value=grid_data):
        await _sync_sessions(db_session)

    result = await db_session.execute(select(Session).where(Session.session_id == "sess-terminal-race"))
    sessions = result.scalars().all()
    assert len(sessions) == 1
    assert sessions[0].status == SessionStatus.passed


async def test_sync_promotes_ready_run_to_active(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-006",
        connection_target="dev-006",
        name="Reserved Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.reserved,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    run = TestRun(
        name="Ready Run",
        state=RunState.ready,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
            }
        ],
    )
    db_session.add(run)
    await db_session.commit()

    grid_data = _grid_response([_grid_session("sess-6", "dev-006", "reservation_test")])

    with patch("app.services.session_sync.grid_service.get_grid_status", return_value=grid_data):
        await _sync_sessions(db_session)

    await db_session.refresh(run, ["device_reservations"])
    await db_session.refresh(device)
    assert run.state == RunState.active
    assert run.started_at is not None
    assert device.availability_status == DeviceAvailabilityStatus.busy


async def test_sync_restores_reserved_status_after_session_end_for_reserved_run(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-007",
        connection_target="dev-007",
        name="Reserved Return",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.busy,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    run = TestRun(
        name="Reserved Return Run",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
                "excluded": False,
                "exclusion_reason": None,
                "excluded_at": None,
            }
        ],
    )
    session = Session(session_id="sess-7", device_id=device.id, status=SessionStatus.running)
    db_session.add_all([run, session])
    await db_session.commit()

    with patch("app.services.session_sync.grid_service.get_grid_status", return_value=_grid_response([])):
        await _sync_sessions(db_session)

    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.reserved


async def test_sync_stops_deferred_unhealthy_device_after_session_end(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-008",
        connection_target="dev-008",
        name="Deferred Stop",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.busy,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    run = TestRun(
        name="Deferred Stop Run",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
                "excluded": False,
                "exclusion_reason": None,
                "excluded_at": None,
            }
        ],
    )
    session = Session(session_id="sess-8", device_id=device.id, status=SessionStatus.running)
    db_session.add_all([run, session])
    await db_session.commit()

    await handle_health_failure(db_session, device, source="device_checks", reason="ADB not responsive")

    with patch("app.services.session_sync.grid_service.get_grid_status", return_value=_grid_response([])):
        await _sync_sessions(db_session)

    await db_session.refresh(device)
    await db_session.refresh(run, ["device_reservations"])
    assert device.availability_status == DeviceAvailabilityStatus.offline
    assert run.reserved_devices is not None
    assert run.reserved_devices[0]["excluded"] is True


async def test_sync_tracks_probe_sessions(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-probe",
        connection_target="dev-probe",
        name="Probe Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    grid_data = _grid_response(
        [
            {
                "sessionId": "probe-sess-1",
                "capabilities": {
                    "platformName": "android",
                    "appium:udid": "dev-probe",
                    "gridfleet:probeSession": True,
                    "gridfleet:testName": "__gridfleet_probe__",
                },
            }
        ]
    )

    with patch("app.services.session_sync.grid_service.get_grid_status", return_value=grid_data):
        await _sync_sessions(db_session)

    result = await db_session.execute(select(Session))
    sessions = result.scalars().all()
    assert len(sessions) == 1
    assert sessions[0].session_id == "probe-sess-1"
    assert sessions[0].test_name == "__gridfleet_probe__"
    assert sessions[0].requested_capabilities == {
        "platformName": "android",
        "appium:udid": "dev-probe",
        "gridfleet:probeSession": True,
        "gridfleet:testName": "__gridfleet_probe__",
    }
    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.busy


async def test_sync_ignores_reserved_placeholder_sessions(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-reserved",
        connection_target="emulator-5554",
        name="Reserved Placeholder Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.reserved,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.emulator,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    grid_data = _grid_response([_grid_session("reserved", "emulator-5554")])

    with patch("app.services.session_sync.grid_service.get_grid_status", return_value=grid_data):
        await _sync_sessions(db_session)

    result = await db_session.execute(select(Session).where(Session.session_id == "reserved"))
    assert result.scalar_one_or_none() is None
    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.reserved
