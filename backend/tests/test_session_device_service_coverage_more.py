from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import encode_cursor
from app.devices.models import ConnectionType, DeviceHold, DeviceOperationalState, DeviceType, HardwareHealthStatus
from app.devices.schemas.device import HardwareTelemetryState
from app.devices.schemas.filters import DeviceQueryFilters
from app.devices.services import service as device_service
from app.runs.models import TestRun
from app.sessions import service as session_service
from app.sessions.models import Session, SessionStatus
from tests.helpers import create_device_record

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_session_listing_cursor_filters_and_payload_helpers(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="session-list-001",
        connection_target="session-list-target",
        name="Session List Device",
        operational_state=DeviceOperationalState.available,
    )
    now = datetime.now(UTC)
    sessions = [
        Session(
            session_id="sess-old",
            device_id=device.id,
            test_name="old",
            status=SessionStatus.passed,
            started_at=now - timedelta(minutes=3),
            ended_at=now - timedelta(minutes=2),
            requested_pack_id="appium-uiautomator2",
            requested_platform_id="android_mobile",
        ),
        Session(
            session_id="sess-new",
            device_id=None,
            test_name="new",
            status=SessionStatus.error,
            started_at=now - timedelta(minutes=1),
            ended_at=now,
            requested_pack_id="appium-uiautomator2",
            requested_platform_id="android_mobile",
            requested_device_type=DeviceType.real_device,
            requested_connection_type=ConnectionType.usb,
            requested_capabilities={"browserName": "Chrome"},
            error_type="driver",
            error_message="boom",
        ),
    ]
    db_session.add_all(sessions)
    await db_session.commit()

    listed, total = await session_service.list_sessions(
        db_session,
        status=SessionStatus.error,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        started_after=now - timedelta(minutes=2),
        started_before=now + timedelta(seconds=1),
        limit=10,
        sort_by="duration",
        sort_dir="asc",
    )
    assert total == 1
    assert [session.session_id for session in listed] == ["sess-new"]

    first_page = await session_service.list_sessions_cursor(db_session, limit=1)
    assert first_page.next_cursor is not None
    newer_page = await session_service.list_sessions_cursor(
        db_session,
        cursor=encode_cursor(sessions[0].started_at, sessions[0].id),
        direction="newer",
        limit=1,
    )
    assert newer_page.items

    filtered_page = await session_service.list_sessions_cursor(
        db_session,
        device_id=device.id,
        status=SessionStatus.passed,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        started_after=now - timedelta(minutes=4),
        started_before=now,
        limit=10,
    )
    assert [session.session_id for session in filtered_page.items] == ["sess-old"]
    device_sessions = await session_service.get_device_sessions(db_session, device.id)
    assert [session.session_id for session in device_sessions] == ["sess-old"]
    heatmap_rows = await session_service.get_device_session_outcome_heatmap_rows(db_session, device.id, days=1)
    assert heatmap_rows == [(sessions[0].started_at, SessionStatus.passed)]

    empty_page = await session_service.list_sessions_cursor(
        db_session,
        cursor=encode_cursor(now - timedelta(days=1), sessions[0].id),
    )
    assert empty_page.items == []

    started_payload = session_service.build_session_started_event_payload(sessions[1], device=None, run_id="run-1")
    ended_payload = session_service.build_session_ended_event_payload(sessions[1], device=None)
    assert started_payload["device_id"] is None
    assert started_payload["requested_device_type"] == "real_device"
    assert ended_payload["error_type"] == "driver"
    assert ended_payload["error_message"] == "boom"


async def test_register_and_finish_session_guard_paths(
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="session-register-001",
        connection_target="register-target",
        name="Session Register Device",
        operational_state=DeviceOperationalState.available,
    )

    with pytest.raises(ValueError, match="No matching device"):
        await session_service.register_session(
            db_session,
            session_id="missing-target",
            test_name="missing",
            connection_target="unknown-target",
        )

    running = await session_service.register_session(
        db_session,
        session_id="running-no-target",
        test_name="running",
    )
    same = await session_service.register_session(
        db_session,
        session_id="running-no-target",
        test_name="ignored",
    )
    assert same.id == running.id

    terminal = await session_service.register_session(
        db_session,
        session_id="terminal-session",
        test_name="terminal",
        device_id=device.id,
        status=SessionStatus.failed,
        error_type="setup",
        error_message="bad caps",
    )
    assert terminal.ended_at is not None

    assert await session_service.mark_session_finished(db_session, "does-not-exist") is None
    already = await session_service.mark_session_finished(db_session, "terminal-session")
    assert already is not None
    assert already.id == terminal.id

    live = Session(session_id="finish-device", device_id=device.id, status=SessionStatus.running)
    db_session.add(live)
    await db_session.commit()
    monkeypatch.setattr(
        "app.sessions.service.lifecycle_policy.handle_session_finished",
        AsyncMock(),
    )
    finished = await session_service.mark_session_finished(db_session, "finish-device")
    assert finished is not None
    assert finished.ended_at is not None

    monkeypatch.setattr(
        "app.sessions.service.lifecycle_policy.complete_deferred_stop_if_session_ended",
        AsyncMock(),
    )
    run = TestRun(name="terminal reservation", requirements=[])
    db_session.add(run)
    await db_session.flush()
    monkeypatch.setattr(
        "app.sessions.service.run_service.get_device_reservation_with_entry",
        AsyncMock(return_value=(run, SimpleNamespace(excluded=False))),
    )
    monkeypatch.setattr(
        "app.sessions.service.run_service.reservation_entry_is_excluded",
        lambda _entry: False,
    )
    terminal_with_run = await session_service.register_session(
        db_session,
        session_id="terminal-reserved-session",
        test_name="terminal reserved",
        device_id=device.id,
        status=SessionStatus.failed,
    )
    assert terminal_with_run.run_id is not None

    assert await session_service.update_session_status(db_session, "missing-status", SessionStatus.passed) is None
    unchanged = await session_service.update_session_status(db_session, "running-no-target", SessionStatus.running)
    assert unchanged is not None
    assert unchanged.ended_at is None


async def test_mark_session_finished_commits_when_device_row_vanished(monkeypatch: pytest.MonkeyPatch) -> None:
    session = SimpleNamespace(session_id="vanished-device", device_id=__import__("uuid").uuid4(), ended_at=None)
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    monkeypatch.setattr(session_service, "get_session", AsyncMock(return_value=session))

    result = await session_service.mark_session_finished(db, "vanished-device")

    assert result is session
    db.commit.assert_awaited_once()


async def test_session_service_missing_and_insert_conflict_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    monkeypatch.setattr(session_service, "get_session", AsyncMock(side_effect=[None, None, None, None]))
    monkeypatch.setattr(session_service, "_lock_resolved_device_for_session", AsyncMock(return_value=None))

    with pytest.raises(ValueError, match="Session insert conflicted"):
        await session_service.register_session(db, session_id="conflict", test_name="conflict")

    assert await session_service.mark_session_finished(db, "missing") is None
    assert await session_service.update_session_status(db, "missing", SessionStatus.passed) is None


async def test_register_terminal_session_with_device_runs_deferred_stop_completion(
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="terminal-device-session",
        connection_target="terminal-device-session",
        name="Terminal Device Session",
    )
    monkeypatch.setattr(
        session_service.run_service,
        "get_device_reservation_with_entry",
        AsyncMock(return_value=(None, None)),
    )
    complete = AsyncMock()
    monkeypatch.setattr(session_service.lifecycle_policy, "complete_deferred_stop_if_session_ended", complete)

    session = await session_service.register_session(
        db_session,
        session_id="terminal-device-session-id",
        test_name="terminal device",
        device_id=device.id,
        status=SessionStatus.failed,
    )

    assert session.device_id == device.id
    complete.assert_awaited_once()


async def test_session_service_private_resolution_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    device_id = __import__("uuid").uuid4()
    node = SimpleNamespace(active_connection_target="agent-target")
    device = SimpleNamespace(id=device_id, connection_target="primary-target", appium_node=node)

    assert session_service._device_matches_session_connection(device, None) is True
    assert session_service._device_matches_session_connection(device, "primary-target") is True
    assert session_service._device_matches_session_connection(device, "agent-target") is True
    assert session_service._device_matches_session_connection(device, "other") is False

    monkeypatch.setattr(session_service, "_resolve_device_for_session", AsyncMock(return_value=None))
    assert (
        await session_service._lock_resolved_device_for_session(
            AsyncMock(),
            device_id=device_id,
            connection_target="primary-target",
        )
        is None
    )

    monkeypatch.setattr(session_service, "_resolve_device_for_session", AsyncMock(return_value=device))
    monkeypatch.setattr(
        session_service.device_locking, "lock_device", AsyncMock(return_value=SimpleNamespace(id=device_id))
    )
    locked = await session_service._lock_resolved_device_for_session(
        AsyncMock(),
        device_id=device_id,
        connection_target="different",
    )
    assert locked is not None

    monkeypatch.setattr(session_service.device_locking, "lock_device", AsyncMock(return_value=device))
    assert (
        await session_service._lock_resolved_device_for_session(
            AsyncMock(),
            device_id=None,
            connection_target="agent-target",
        )
        is device
    )

    wrong_locked = SimpleNamespace(id=__import__("uuid").uuid4(), connection_target="other")
    monkeypatch.setattr(session_service.device_locking, "lock_device", AsyncMock(return_value=wrong_locked))
    assert (
        await session_service._lock_resolved_device_for_session(
            AsyncMock(),
            device_id=device_id,
            connection_target="missing",
        )
        is None
    )


async def test_device_service_filters_pagination_update_and_delete_branches(
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    available = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="device-filter-available",
        connection_target="device-filter-available",
        name="Alpha Device",
        operational_state=DeviceOperationalState.available,
        tags={"team": "qa"},
        hardware_health_status=HardwareHealthStatus.warning,
    )
    maintenance = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="device-filter-maint",
        connection_target="device-filter-maint",
        name="Beta Device",
        operational_state=DeviceOperationalState.offline,
        hold=DeviceHold.maintenance,
        tags={"team": "ops"},
    )

    monkeypatch.setattr(
        "app.devices.services.service.run_service.get_device_reservation_map", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        "app.devices.services.service.device_readiness.assess_device_async",
        AsyncMock(return_value=SimpleNamespace(readiness_state="ready")),
    )
    monkeypatch.setattr(
        "app.devices.services.service.lifecycle_policy.build_lifecycle_policy",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        "app.devices.services.service.lifecycle_policy.build_lifecycle_policy_summary",
        lambda _policy: {"state": "healthy"},
    )
    monkeypatch.setattr(
        "app.devices.services.service.device_health.build_public_summary",
        lambda _device: {"healthy": True},
    )
    monkeypatch.setattr(
        "app.devices.services.service.hardware_telemetry.current_hardware_health_status", lambda _device: None
    )
    monkeypatch.setattr(
        "app.devices.services.service.device_attention.compute_needs_attention",
        lambda *_args, **_kwargs: False,
    )

    filters = DeviceQueryFilters(
        status="available",
        host_id=available.host_id,
        search="Alpha",
        tags={"team": "qa"},
        needs_attention=False,
        sort_by="name",
        sort_dir="asc",
    )
    devices = await device_service.list_devices_by_filters(db_session, filters)
    assert [device.id for device in devices] == [available.id]

    page, total = await device_service.list_devices_paginated(db_session, filters, limit=1, offset=0)
    assert total == 1
    assert [device.id for device in page] == [available.id]
    assert await device_service.count_devices_by_filters(db_session, filters) == 1

    telemetry_filters = DeviceQueryFilters(hardware_telemetry_state=HardwareTelemetryState.fresh)
    monkeypatch.setattr(
        "app.devices.services.service.hardware_telemetry.hardware_telemetry_state_for_device",
        lambda device: HardwareTelemetryState.fresh if device.id == available.id else HardwareTelemetryState.stale,
    )
    telemetry_devices = await device_service.list_devices_by_filters(db_session, telemetry_filters)
    assert [device.id for device in telemetry_devices] == [available.id]

    page, total = await device_service.list_devices_paginated(
        db_session,
        DeviceQueryFilters(platform_id="android_mobile"),
        limit=10,
        offset=0,
    )
    assert total >= 2
    assert any(device.id == available.id for device in page)
    assert (
        await device_service.count_devices_by_filters(
            db_session,
            DeviceQueryFilters(platform_id="android_mobile"),
        )
        >= 2
    )

    maintenance_devices = await device_service.list_devices_by_filters(
        db_session,
        DeviceQueryFilters(status="maintenance"),
    )
    assert [device.id for device in maintenance_devices] == [maintenance.id]

    reserved = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="device-filter-reserved",
        connection_target="device-filter-reserved",
        name="Reserved Device",
        operational_state=DeviceOperationalState.available,
        hold=DeviceHold.reserved,
    )
    verifying = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="device-filter-verifying",
        connection_target="device-filter-verifying",
        name="Verifying Device",
        operational_state=DeviceOperationalState.verifying,
    )
    await db_session.commit()
    reserved_devices = await device_service.list_devices_by_filters(db_session, DeviceQueryFilters(status="reserved"))
    verifying_devices = await device_service.list_devices_by_filters(
        db_session,
        DeviceQueryFilters(status="verifying"),
    )
    assert [device.id for device in reserved_devices] == [reserved.id]
    assert [device.id for device in verifying_devices] == [verifying.id]

    assert await device_service.get_device(db_session, available.id) is not None
    assert (
        await device_service.update_device(
            db_session, __import__("uuid").uuid4(), object(), enforce_patch_contract=False
        )
        is None
    )

    assert await device_service.delete_device(db_session, __import__("uuid").uuid4()) is False
    monkeypatch.setattr("app.devices.services.service._stop_node", AsyncMock(side_effect=RuntimeError("stop failed")))
    monkeypatch.setattr("app.devices.services.service._lock_device_for_delete", AsyncMock(return_value=maintenance))
    fake_running = SimpleNamespace(id=maintenance.id, appium_node=SimpleNamespace(observed_running=True))
    relocked = await device_service._stop_running_node_for_delete(db_session, fake_running, maintenance.id)
    assert relocked is not None

    monkeypatch.setattr("app.devices.services.service._stop_node", AsyncMock())
    monkeypatch.setattr("app.devices.services.service._lock_device_for_delete", AsyncMock(return_value=None))
    assert await device_service._stop_running_node_for_delete(db_session, fake_running, maintenance.id) is None
