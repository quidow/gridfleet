from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import Mock

from app.devices.models import HardwareChargingState, HardwareHealthStatus, HardwareTelemetrySupportStatus
from app.hosts import service_hardware_telemetry as hardware_telemetry
from app.hosts.service_hardware_telemetry import HardwareTelemetryService
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device_record, drain_handlers, seed_host_named
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def test_apply_telemetry_sample_marks_device_healthy(db_session: AsyncSession) -> None:
    host = await seed_host_named(db_session, "pixel-host-healthy")
    device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="pixel-healthy",
        connection_target="pixel-healthy",
        name="Pixel Healthy",
    )

    svc = HardwareTelemetryService(publisher=Mock(), settings=FakeSettingsReader({}))
    await svc.apply_telemetry_sample(
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


async def test_fold_applies_pushed_telemetry_maps_observed_at(db_session: AsyncSession) -> None:
    host = await seed_host_named(db_session, "pixel-host-fold")
    device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="pixel-fold",
        connection_target="pixel-fold",
        name="Pixel Fold",
    )
    await db_session.commit()

    stamp = "2026-07-10T00:00:00+00:00"
    section = {
        "reported_at": stamp,
        "devices": {
            "pixel-fold": {
                "support_status": "supported",
                "battery_level_percent": 41,
                "battery_temperature_c": 33.5,
                "charging_state": "discharging",
                "observed_at": stamp,
            }
        },
    }
    svc = HardwareTelemetryService(publisher=Mock(), settings=FakeSettingsReader({}))
    await svc.fold_host_device_telemetry(db_session, host.id, section)

    await db_session.refresh(device)
    assert device.battery_level_percent == 41
    assert device.charging_state == HardwareChargingState.discharging
    # observed_at was mapped onto the persisted reported_at column.
    assert device.hardware_telemetry_reported_at is not None
    assert device.hardware_telemetry_reported_at.isoformat() == stamp


async def test_apply_telemetry_sample_requires_consecutive_warning_samples(
    db_session: AsyncSession,
) -> None:
    host = await seed_host_named(db_session, "pixel-host-warn")
    device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="pixel-warn",
        connection_target="pixel-warn",
        name="Pixel Warn",
    )
    _hw_settings = FakeSettingsReader(
        {
            "general.hardware_telemetry_consecutive_samples": 2,
            "general.hardware_temperature_warning_c": 38,
            "general.hardware_temperature_critical_c": 42,
        }
    )
    svc = HardwareTelemetryService(publisher=Mock(), settings=_hw_settings)
    svc_bus = HardwareTelemetryService(publisher=event_bus, settings=_hw_settings)

    await svc.apply_telemetry_sample(
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
    await svc_bus.apply_telemetry_sample(db_session, device, warning_sample)
    await db_session.commit()
    assert device.hardware_health_status == HardwareHealthStatus.healthy

    # A second, distinct observation crosses the two-sample threshold. (The
    # counter counts distinct observations, so this second sample carries a
    # later observed_at than the first.)
    await svc_bus.apply_telemetry_sample(db_session, device, {**warning_sample, "reported_at": "2026-04-16T10:06:00Z"})
    await db_session.commit()
    assert device.hardware_health_status == HardwareHealthStatus.warning

    await drain_handlers(event_bus)
    events, total = await event_bus.get_recent_events_persisted(limit=10)
    assert total == 1
    assert events[0]["type"] == "device.hardware_health_changed"
    assert events[0]["data"]["new_status"] == "warning"


async def test_repushed_identical_sample_does_not_advance_consecutive_streak(
    db_session: AsyncSession,
) -> None:
    """A 60s-gathered sample re-folded across many 10s pushes carries the same
    ``observed_at``; the consecutive-sample counter must count distinct
    observations, not applications. Re-pushing the identical sample must not
    cross the hysteresis threshold on its own."""
    host = await seed_host_named(db_session, "pixel-host-repush")
    device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="pixel-repush",
        connection_target="pixel-repush",
        name="Pixel Repush",
    )
    _hw_settings = FakeSettingsReader(
        {
            "general.hardware_telemetry_consecutive_samples": 2,
            "general.hardware_temperature_warning_c": 38,
            "general.hardware_temperature_critical_c": 42,
        }
    )
    svc = HardwareTelemetryService(publisher=Mock(), settings=_hw_settings)

    await svc.apply_telemetry_sample(
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
    # Same sample folded three times (three 10s pushes of one 60s gather).
    for _ in range(3):
        await svc.apply_telemetry_sample(db_session, device, warning_sample)
        await db_session.commit()
        assert device.hardware_health_status == HardwareHealthStatus.healthy

    # A genuinely new observation (different observed_at) advances the streak.
    await svc.apply_telemetry_sample(
        db_session,
        device,
        {**warning_sample, "reported_at": "2026-04-16T10:06:00Z"},
    )
    await db_session.commit()
    assert device.hardware_health_status == HardwareHealthStatus.warning


async def test_hardware_telemetry_state_distinguishes_unknown_fresh_stale_and_unsupported(
    db_session: AsyncSession,
) -> None:
    host = await seed_host_named(db_session, "pixel-host-states")
    unknown = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="unknown-device",
        connection_target="unknown-device",
        name="Unknown Device",
    )
    supported = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="supported-device",
        connection_target="supported-device",
        name="Supported Device",
    )
    unsupported = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="unsupported-device",
        connection_target="unsupported-device",
        name="Unsupported Device",
    )

    supported.hardware_telemetry_support_status = HardwareTelemetrySupportStatus.supported
    supported.hardware_telemetry_reported_at = datetime.now(UTC) - timedelta(hours=1)
    unsupported.hardware_telemetry_support_status = HardwareTelemetrySupportStatus.unsupported
    unsupported.hardware_telemetry_reported_at = datetime.now(UTC)
    await db_session.commit()

    _stale_settings = FakeSettingsReader({"general.hardware_telemetry_stale_timeout_sec": 60})

    assert hardware_telemetry.hardware_telemetry_state_for_device(unknown, settings=_stale_settings).value == "unknown"
    assert hardware_telemetry.hardware_telemetry_state_for_device(supported, settings=_stale_settings).value == "stale"
    assert (
        hardware_telemetry.hardware_telemetry_state_for_device(unsupported, settings=_stale_settings).value
        == "unsupported"
    )

    supported.hardware_telemetry_reported_at = datetime.now(UTC)
    assert hardware_telemetry.hardware_telemetry_state_for_device(supported, settings=_stale_settings).value == "fresh"


async def test_hardware_telemetry_state_returns_unsupported_for_emulator(
    db_session: AsyncSession,
) -> None:
    host = await seed_host_named(db_session, "pixel-host-emulator")
    emulator = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="emulator-5554",
        connection_target="emulator-5554",
        name="Emulator",
        device_type="emulator",
    )

    assert (
        hardware_telemetry.hardware_telemetry_state_for_device(emulator, settings=FakeSettingsReader({})).value
        == "unsupported"
    )
