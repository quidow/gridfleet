from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import event_bus
from app.models.device import (
    HardwareChargingState,
    HardwareHealthStatus,
    HardwareTelemetrySupportStatus,
)
from app.services import hardware_telemetry
from app.settings import settings_service
from tests.helpers import create_device_record, create_host


async def test_apply_telemetry_sample_marks_device_healthy(db_session: AsyncSession, client: AsyncClient) -> None:
    host = await create_host(client)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="pixel-healthy",
        connection_target="pixel-healthy",
        name="Pixel Healthy",
    )

    await hardware_telemetry.apply_telemetry_sample(
        db_session,
        device,
        {
            "battery_level_percent": 92,
            "battery_temperature_c": 33.4,
            "charging_state": "charging",
            "support_status": "supported",
            "reported_at": "2026-04-16T10:00:00Z",
        },
    )
    await db_session.commit()

    assert device.battery_level_percent == 92
    assert device.battery_temperature_c == 33.4
    assert device.charging_state == HardwareChargingState.charging
    assert device.hardware_telemetry_support_status == HardwareTelemetrySupportStatus.supported
    assert device.hardware_health_status == HardwareHealthStatus.healthy


async def test_apply_telemetry_sample_requires_consecutive_warning_samples(
    db_session: AsyncSession,
    client: AsyncClient,
) -> None:
    host = await create_host(client)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="pixel-warn",
        connection_target="pixel-warn",
        name="Pixel Warn",
    )
    settings_service._cache["general.hardware_telemetry_consecutive_samples"] = 2
    settings_service._cache["general.hardware_temperature_warning_c"] = 38
    settings_service._cache["general.hardware_temperature_critical_c"] = 42

    await hardware_telemetry.apply_telemetry_sample(
        db_session,
        device,
        {
            "battery_level_percent": 88,
            "battery_temperature_c": 34.0,
            "charging_state": "charging",
            "support_status": "supported",
            "reported_at": "2026-04-16T10:00:00Z",
        },
    )
    await db_session.commit()
    assert device.hardware_health_status == HardwareHealthStatus.healthy

    warning_sample = {
        "battery_level_percent": 87,
        "battery_temperature_c": 39.2,
        "charging_state": "charging",
        "support_status": "supported",
        "reported_at": "2026-04-16T10:05:00Z",
    }
    await hardware_telemetry.apply_telemetry_sample(db_session, device, warning_sample)
    await db_session.commit()
    assert device.hardware_health_status == HardwareHealthStatus.healthy

    await hardware_telemetry.apply_telemetry_sample(db_session, device, warning_sample)
    await db_session.commit()
    assert device.hardware_health_status == HardwareHealthStatus.warning

    await event_bus.drain_handlers()
    events, total = await event_bus.get_recent_events_persisted(limit=10)
    assert total == 1
    assert events[0]["type"] == "device.hardware_health_changed"
    assert events[0]["data"]["new_status"] == "warning"


async def test_hardware_telemetry_state_distinguishes_unknown_fresh_stale_and_unsupported(
    db_session: AsyncSession,
    client: AsyncClient,
) -> None:
    host = await create_host(client)
    unknown = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="unknown-device",
        connection_target="unknown-device",
        name="Unknown Device",
    )
    supported = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="supported-device",
        connection_target="supported-device",
        name="Supported Device",
    )
    unsupported = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="unsupported-device",
        connection_target="unsupported-device",
        name="Unsupported Device",
    )

    supported.hardware_telemetry_support_status = HardwareTelemetrySupportStatus.supported
    supported.hardware_telemetry_reported_at = datetime.now(UTC) - timedelta(hours=1)
    unsupported.hardware_telemetry_support_status = HardwareTelemetrySupportStatus.unsupported
    unsupported.hardware_telemetry_reported_at = datetime.now(UTC)
    await db_session.commit()

    settings_service._cache["general.hardware_telemetry_stale_timeout_sec"] = 60

    assert hardware_telemetry.hardware_telemetry_state_for_device(unknown).value == "unknown"
    assert hardware_telemetry.hardware_telemetry_state_for_device(supported).value == "stale"
    assert hardware_telemetry.hardware_telemetry_state_for_device(unsupported).value == "unsupported"

    supported.hardware_telemetry_reported_at = datetime.now(UTC)
    assert hardware_telemetry.hardware_telemetry_state_for_device(supported).value == "fresh"


async def test_hardware_telemetry_state_returns_unsupported_for_emulator(
    db_session: AsyncSession,
    client: AsyncClient,
) -> None:
    host = await create_host(client)
    emulator = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="emulator-5554",
        connection_target="emulator-5554",
        name="Emulator",
        device_type="emulator",
    )

    assert hardware_telemetry.hardware_telemetry_state_for_device(emulator).value == "unsupported"
