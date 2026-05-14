from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import ConnectionType, Device, DeviceHold, DeviceOperationalState, DeviceType
from app.models.host import Host
from app.models.session import Session, SessionStatus
from app.models.test_run import RunState, TestRun
from app.services.lifecycle_policy import handle_health_failure
from app.services.session_sync import _sync_sessions

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


@pytest.fixture(autouse=True)
def _skip_leader_fencing() -> Iterator[None]:
    """No-op assert_current_leader so unit tests don't need a real leader row."""
    with patch("app.services.session_sync.assert_current_leader"):
        yield


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
        operational_state=DeviceOperationalState.available,
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
    assert device.operational_state == DeviceOperationalState.busy


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
        operational_state=DeviceOperationalState.busy,
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
    assert device.operational_state == DeviceOperationalState.available


async def test_sync_ends_duplicate_running_sessions(db_session: AsyncSession, db_host: Host) -> None:
    """Two Session rows share the same session_id with status=running.

    Reproduces the production crash where session_sync.scalar_one_or_none()
    raised MultipleResultsFound, deadlocking the loop and leaving devices
    stuck busy. ``ux_sessions_session_id_running`` now blocks new duplicates,
    but rows that pre-date the migration can still exist; the loop must
    survive them and end every matching row.
    """
    from sqlalchemy import text

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-dup",
        connection_target="dev-dup",
        name="Duplicate Phone",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    # Drop the partial unique index just for this test so we can simulate the
    # pre-migration state where two ``running`` rows shared a ``session_id``.
    await db_session.execute(text("DROP INDEX ux_sessions_session_id_running"))
    try:
        dup_a = Session(session_id="sess-dup", device_id=device.id, status=SessionStatus.running)
        dup_b = Session(session_id="sess-dup", device_id=device.id, status=SessionStatus.running)
        db_session.add_all([dup_a, dup_b])
        await db_session.commit()

        grid_data = _grid_response([])

        with patch("app.services.session_sync.grid_service.get_grid_status", return_value=grid_data):
            await _sync_sessions(db_session)

        result = await db_session.execute(select(Session).where(Session.session_id == "sess-dup"))
        rows = result.scalars().all()
        assert len(rows) == 2
        for row in rows:
            assert row.status == SessionStatus.passed
            assert row.ended_at is not None

        await db_session.refresh(device)
        assert device.operational_state == DeviceOperationalState.available
    finally:
        # Force any lingering duplicate ``running`` rows to a terminal state
        # before recreating the partial unique index. Without this, a failure
        # in the test body (e.g. the loop tolerance regression returns) would
        # leave duplicates in place and the CREATE INDEX below would raise
        # IntegrityError, masking the original assertion error.
        await db_session.rollback()
        await db_session.execute(
            text(
                "UPDATE sessions SET status = 'error', ended_at = NOW() "
                "WHERE session_id = 'sess-dup' AND status = 'running' AND ended_at IS NULL"
            )
        )
        await db_session.execute(
            text(
                "CREATE UNIQUE INDEX ux_sessions_session_id_running ON sessions (session_id) "
                "WHERE status = 'running' AND ended_at IS NULL"
            )
        )
        await db_session.commit()


async def test_sync_ends_duplicate_running_sessions_across_devices(db_session: AsyncSession, db_host: Host) -> None:
    """Two ``running`` rows share a ``session_id`` but reference different devices.

    Even though ``ux_sessions_session_id_running`` blocks this in fresh
    installs, legacy data (pre-migration races, agent reassignments) can
    leave a single ``session_id`` mapped to multiple device rows. The
    ended-session sweep must move ``operational_state`` off busy on every
    affected device, not only on the one that ``known_running`` happened
    to retain after dict overwrite.
    """
    from sqlalchemy import text

    device_a = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-dup-multi-a",
        connection_target="dev-dup-multi-a",
        name="Duplicate Phone A",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    device_b = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-dup-multi-b",
        connection_target="dev-dup-multi-b",
        name="Duplicate Phone B",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add_all([device_a, device_b])
    await db_session.flush()

    await db_session.execute(text("DROP INDEX ux_sessions_session_id_running"))
    try:
        dup_a = Session(session_id="sess-dup-multi", device_id=device_a.id, status=SessionStatus.running)
        dup_b = Session(session_id="sess-dup-multi", device_id=device_b.id, status=SessionStatus.running)
        db_session.add_all([dup_a, dup_b])
        await db_session.commit()

        with patch("app.services.session_sync.grid_service.get_grid_status", return_value=_grid_response([])):
            await _sync_sessions(db_session)

        rows = (await db_session.execute(select(Session).where(Session.session_id == "sess-dup-multi"))).scalars().all()
        assert len(rows) == 2
        assert all(row.ended_at is not None for row in rows)

        await db_session.refresh(device_a)
        await db_session.refresh(device_b)
        assert device_a.operational_state != DeviceOperationalState.busy, (
            "every device referenced by a duplicate ended session must move off busy"
        )
        assert device_b.operational_state != DeviceOperationalState.busy
    finally:
        await db_session.rollback()
        await db_session.execute(
            text(
                "UPDATE sessions SET status = 'error', ended_at = NOW() "
                "WHERE session_id = 'sess-dup-multi' AND status = 'running' AND ended_at IS NULL"
            )
        )
        await db_session.execute(
            text(
                "CREATE UNIQUE INDEX ux_sessions_session_id_running ON sessions (session_id) "
                "WHERE status = 'running' AND ended_at IS NULL"
            )
        )
        await db_session.commit()


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
        operational_state=DeviceOperationalState.busy,
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
    assert refreshed_device.operational_state == DeviceOperationalState.available


async def test_sync_marks_late_ended_session_for_cancelled_run_as_error(
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.helpers import create_device_record, create_reserved_run

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="late-ended-cancel",
        connection_target="late-ended-cancel",
        name="Late Ended Cancel",
        operational_state=DeviceOperationalState.busy,
    )
    run = await create_reserved_run(
        db_session,
        name="Late Ended Cancel Run",
        devices=[device],
        state=RunState.cancelled,
        mark_released=True,
    )
    session = Session(
        session_id="late-ended-session",
        device_id=device.id,
        run_id=run.id,
        test_name="test_late_ended",
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    monkeypatch.setattr(
        "app.services.session_sync.grid_service.get_grid_status",
        AsyncMock(return_value={"value": {"ready": True, "nodes": []}}),
    )

    await _sync_sessions(db_session)

    await db_session.refresh(session)
    assert session.status == SessionStatus.error
    assert session.error_type == "run_released"
    assert session.error_message == "Run ended while session was still running (cancelled)"
    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available


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
        operational_state=DeviceOperationalState.available,
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
    assert device.operational_state == DeviceOperationalState.busy


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
        operational_state=DeviceOperationalState.busy,
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
    assert device.operational_state == DeviceOperationalState.busy  # sess-4b still running


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
        operational_state=DeviceOperationalState.busy,
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
        operational_state=DeviceOperationalState.busy,
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


async def test_sync_backfills_started_at_for_active_run(db_session: AsyncSession, db_host: Host) -> None:
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
        hold=DeviceHold.reserved,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    run = TestRun(
        name="Active Run",
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
    assert device.operational_state == DeviceOperationalState.busy


async def test_sync_preserves_reserved_hold_after_session_end_for_reserved_run(
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
        operational_state=DeviceOperationalState.busy,
        hold=DeviceHold.reserved,
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
    assert device.hold == DeviceHold.reserved


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
        operational_state=DeviceOperationalState.busy,
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
    assert device.operational_state == DeviceOperationalState.offline
    assert run.reserved_devices is not None
    assert run.reserved_devices[0]["excluded"] is True


async def test_sync_restores_busy_when_deferred_stop_dropped_for_healthy_device(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """When `handle_session_finished` drops a deferred-stop intent because the
    device is currently healthy (defense-in-depth branch), it returns False so
    `_on_session_end` falls through to `ready_operational_state`.
    The device must end up `available`, not stuck at `busy`."""
    from app.models.appium_node import AppiumDesiredState, AppiumNode
    from app.services import device_health

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-deferred-recovered",
        connection_target="dev-deferred-recovered",
        name="Deferred Recovered",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4790,
        grid_url="http://hub:4444",
        desired_state=AppiumDesiredState.running,
        desired_port=4790,
        pid=0,
        active_connection_target="",
    )
    db_session.add(node)
    session = Session(session_id="sess-deferred-recovered", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    # Defer a stop (simulates an earlier transient failure during this session).
    await handle_health_failure(db_session, device, source="node_health", reason="Probe failed")

    # Health later recovers - seed derived health to healthy. Recovery wiring
    # would normally clear stop_pending here, but this test exercises the
    # defense-in-depth path where it didn't, so we leave stop_pending=True.)
    await device_health.apply_node_state_transition(
        db_session,
        device,
        health_running=None,
        health_state=None,
        mark_offline=False,
    )
    await device_health.update_device_checks(db_session, device, healthy=True, summary="Healthy")
    await db_session.commit()

    # Session ends — Grid no longer reports it.
    with patch("app.services.session_sync.grid_service.get_grid_status", return_value=_grid_response([])):
        await _sync_sessions(db_session)

    await db_session.refresh(device)
    # Intent was cleared but device should be RESTORED to available, not stopped.
    assert device.operational_state == DeviceOperationalState.available
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state["stop_pending"] is False


async def test_sync_does_not_restore_busy_when_fresh_session_inserted_after_precheck(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Race fix: a fresh client session inserted between the outer
    ``still_running`` check and the locked restore must NOT be restored away
    from busy. ``handle_session_finished`` returns ``NO_PENDING`` for
    no-deferred-stop devices without doing the locked Session check, so the
    restore guard performs its own locked recheck.
    """
    from app.services import session_sync

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dev-race-restore",
        connection_target="dev-race-restore",
        name="Race Restore",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        # No deferred-stop intent — ``handle_session_finished`` will hit the
        # NO_PENDING fast-path without checking running sessions under lock.
        lifecycle_policy_state={"stop_pending": False, "last_action": "idle"},
    )
    db_session.add(device)
    await db_session.flush()

    # The "old" session that the outer loop will see as ended.
    old_session = Session(session_id="sess-old-ending", device_id=device.id, status=SessionStatus.running)
    db_session.add(old_session)
    await db_session.commit()

    real_handle = session_sync.lifecycle_policy.handle_session_finished

    async def _handle_then_insert_fresh(db: AsyncSession, dev: Device) -> object:
        # Simulate: between the outer running-set probe and the restore guard,
        # a fresh client session is inserted (e.g. a new POST /api/sessions
        # arriving on a different worker). The new session is committed so
        # the locked recheck inside the restore guard observes it.
        outcome = await real_handle(db, dev)
        new_session = Session(session_id="sess-new-fresh", device_id=dev.id, status=SessionStatus.running)
        db.add(new_session)
        await db.commit()
        return outcome

    monkeypatch.setattr(
        session_sync.lifecycle_policy,
        "handle_session_finished",
        _handle_then_insert_fresh,
    )

    # Old session leaves the Grid (not in active map), triggering ended-session processing.
    with patch("app.services.session_sync.grid_service.get_grid_status", return_value=_grid_response([])):
        await session_sync._sync_sessions(db_session)

    await db_session.refresh(device)
    # The race-prone restore would have moved the device to ``available``.
    # Correct behavior: leave it ``busy`` for the fresh session.
    assert device.operational_state == DeviceOperationalState.busy

    # Sanity check the simulated fresh session is the reason.
    fresh = await db_session.execute(select(Session).where(Session.session_id == "sess-new-fresh"))
    assert fresh.scalar_one_or_none() is not None


async def test_sync_does_not_track_probe_sessions(db_session: AsyncSession, db_host: Host) -> None:
    """Probe sessions are filtered out and never persisted as real Session rows."""
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
        operational_state=DeviceOperationalState.available,
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
    assert sessions == []

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available


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
        hold=DeviceHold.reserved,
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
    assert device.hold == DeviceHold.reserved


async def test_sweep_clears_stale_stop_pending_for_devices_without_sessions(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.grid import service as grid_service
    from app.services import session_sync
    from app.services.lifecycle_policy import handle_health_failure

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-stuck-stop-sweep",
        connection_target="policy-stuck-stop-sweep",
        name="Stuck Deferred Stop Sweep Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    session = Session(
        session_id="sess-stuck-stop-sweep",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    result = await handle_health_failure(db_session, device, source="device_checks", reason="ADB not responsive")
    assert result == "deferred"

    # Simulate the historical bug: a session ended directly in the DB without the helper.
    session.status = SessionStatus.passed
    session.ended_at = datetime.now(UTC)
    await db_session.commit()

    await db_session.refresh(device)
    assert device.lifecycle_policy_state["stop_pending"] is True

    async def _fake_grid_status() -> dict:
        return {"value": {"ready": True, "nodes": []}}

    monkeypatch.setattr(grid_service, "get_grid_status", _fake_grid_status)

    await session_sync._sync_sessions(db_session)

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False


async def test_sweep_runs_when_grid_is_unreachable(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runbook promises one-poll healing for stale ``stop_pending`` rows.

    Tying the sweep to Grid availability would silently weaken that guarantee
    during Grid outages, when stale rows still need to be healed because the
    sweep relies on DB state only. Audit P2 — sweep must run independent of
    Grid status.
    """
    from app.grid import service as grid_service
    from app.services import session_sync

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-sweep-grid-down",
        connection_target="policy-sweep-grid-down",
        name="Sweep Grid Down",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    session = Session(
        session_id="sess-sweep-grid-down",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    result = await handle_health_failure(db_session, device, source="device_checks", reason="ADB hung")
    assert result == "deferred"

    session.status = SessionStatus.passed
    session.ended_at = datetime.now(UTC)
    await db_session.commit()

    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state["stop_pending"] is True

    async def _grid_unreachable() -> dict[str, Any]:
        # Shape that triggers the early-return branch in _sync_sessions:
        # ready=False AND an "error" key present.
        return {"value": {"ready": False}, "error": "connection refused"}

    monkeypatch.setattr(grid_service, "get_grid_status", _grid_unreachable)

    await session_sync._sync_sessions(db_session)

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False, (
        "sweep must heal stale stop_pending rows even when Grid is unreachable"
    )
