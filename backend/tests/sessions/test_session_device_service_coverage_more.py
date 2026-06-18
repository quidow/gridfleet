from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import encode_cursor
from app.devices.models import (
    ConnectionType,
    DeviceOperationalState,
    DeviceReservation,
    DeviceType,
    HardwareHealthStatus,
)
from app.devices.schemas.device import HardwareTelemetryState
from app.devices.schemas.filters import DeviceQueryFilters
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from app.runs.models import RunState, TestRun
from app.sessions import service as session_service
from app.sessions.models import Session, SessionStatus
from app.sessions.service import SessionCrudService
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device_record
from tests.helpers import test_event_bus as event_bus

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

    crud = SessionCrudService(publisher=Mock(), lifecycle=AsyncMock())
    listed, total = await crud.list_sessions(
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

    first_page = await crud.list_sessions_cursor(db_session, limit=1)
    assert first_page.next_cursor is not None
    newer_page = await crud.list_sessions_cursor(
        db_session,
        cursor=encode_cursor(sessions[0].started_at, sessions[0].id),
        direction="newer",
        limit=1,
    )
    assert newer_page.items

    filtered_page = await crud.list_sessions_cursor(
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
    heatmap_rows = await crud.get_device_session_outcome_heatmap_rows(db_session, device.id, days=1)
    assert heatmap_rows == [(sessions[0].started_at, SessionStatus.passed)]

    empty_page = await crud.list_sessions_cursor(
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
        operational_state=DeviceOperationalState.maintenance,
        tags={"team": "ops"},
    )

    monkeypatch.setattr(
        "app.devices.services.service.device_readiness.assess_device_async",
        AsyncMock(return_value=SimpleNamespace(readiness_state="ready")),
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

    crud = DeviceCrudService(
        settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
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
    devices = await crud.list_devices_by_filters(db_session, filters)
    assert [device.id for device in devices] == [available.id]

    page, total = await crud.list_devices_paginated(db_session, filters, limit=1, offset=0)
    assert total == 1
    assert [device.id for device in page] == [available.id]
    assert await crud.count_devices_by_filters(db_session, filters) == 1

    telemetry_filters = DeviceQueryFilters(hardware_telemetry_state=HardwareTelemetryState.fresh)
    monkeypatch.setattr(
        "app.devices.services.service.hardware_telemetry.hardware_telemetry_state_for_device",
        lambda device, settings=None: (
            HardwareTelemetryState.fresh if device.id == available.id else HardwareTelemetryState.stale
        ),
    )
    telemetry_devices = await crud.list_devices_by_filters(db_session, telemetry_filters)
    assert [device.id for device in telemetry_devices] == [available.id]

    page, total = await crud.list_devices_paginated(
        db_session,
        DeviceQueryFilters(platform_id="android_mobile"),
        limit=10,
        offset=0,
    )
    assert total >= 2
    assert any(device.id == available.id for device in page)
    assert (
        await crud.count_devices_by_filters(
            db_session,
            DeviceQueryFilters(platform_id="android_mobile"),
        )
        >= 2
    )

    maintenance_devices = await crud.list_devices_by_filters(
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
    )
    reservation_run = TestRun(name="device-filter-reservation", requirements=[], state=RunState.active)
    db_session.add(reservation_run)
    await db_session.flush()
    db_session.add(
        DeviceReservation(
            run_id=reservation_run.id,
            device_id=reserved.id,
            identity_value=reserved.identity_value,
            connection_target=reserved.connection_target,
            pack_id=reserved.pack_id,
            platform_id=reserved.platform_id,
            os_version=reserved.os_version,
        )
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
    reserved_devices = await crud.list_devices_by_filters(db_session, DeviceQueryFilters(reserved=True))
    verifying_devices = await crud.list_devices_by_filters(
        db_session,
        DeviceQueryFilters(status="verifying"),
    )
    assert [device.id for device in reserved_devices] == [reserved.id]
    assert [device.id for device in verifying_devices] == [verifying.id]

    assert await crud.get_device(db_session, available.id) is not None
    assert (
        await crud.update_device(db_session, __import__("uuid").uuid4(), object(), enforce_patch_contract=False) is None
    )

    assert await crud.delete_device(db_session, __import__("uuid").uuid4()) is False
