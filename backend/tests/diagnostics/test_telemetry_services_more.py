from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.core.errors import AgentCallError
from app.devices.models import (
    ConnectionType,
    DeviceType,
    HardwareChargingState,
    HardwareHealthStatus,
    HardwareTelemetrySupportStatus,
)
from app.devices.schemas.device import HardwareTelemetryState
from app.hosts import (
    service_hardware_telemetry as hardware_telemetry,
)
from app.hosts import (
    service_resource_telemetry as host_resource_telemetry,
)
from app.hosts.service_hardware_telemetry import HardwareTelemetryService
from app.hosts.service_resource_telemetry import HostResourceTelemetryService
from tests.fakes import FakeSettingsReader


class FlushSession:
    def __init__(self) -> None:
        self.flushed = False
        self.committed = False
        self.rolled_back = False
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flushed = True

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def _telemetry_device(**overrides: object) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "name": "Pixel",
        "identity_value": "serial",
        "device_type": DeviceType.real_device,
        "connection_type": ConnectionType.usb,
        "pack_id": "appium-uiautomator2",
        "platform_id": "android_mobile",
        "connection_target": "serial",
        "ip_address": None,
        "host": None,
        "hardware_health_status": HardwareHealthStatus.unknown,
        "hardware_telemetry_support_status": HardwareTelemetrySupportStatus.supported,
        "hardware_telemetry_reported_at": datetime(2026, 5, 1, tzinfo=UTC),
        "battery_level_percent": None,
        "battery_temperature_c": None,
        "charging_state": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_hardware_telemetry_coercion_and_state_derivation() -> None:
    device = _telemetry_device()
    assert hardware_telemetry._coerce_charging_state("charging") == HardwareChargingState.charging
    assert hardware_telemetry._coerce_charging_state("bad") is None
    assert hardware_telemetry._coerce_charging_state(object()) is None
    assert hardware_telemetry._coerce_support_status("unsupported") == HardwareTelemetrySupportStatus.unsupported
    assert hardware_telemetry._coerce_support_status("bad") == HardwareTelemetrySupportStatus.unknown
    assert hardware_telemetry._coerce_support_status(object()) == HardwareTelemetrySupportStatus.unknown
    assert hardware_telemetry._coerce_int(True) is None
    assert hardware_telemetry._coerce_int("12") == 12
    assert hardware_telemetry._coerce_int("bad") is None
    assert hardware_telemetry._coerce_int(object()) is None
    assert hardware_telemetry._coerce_int(12.9) == 12
    assert hardware_telemetry._coerce_float(False) is None
    assert hardware_telemetry._coerce_float("12.5") == 12.5
    assert hardware_telemetry._coerce_float("bad") is None
    assert hardware_telemetry._coerce_float(object()) is None
    assert hardware_telemetry.current_hardware_health_status(device) == HardwareHealthStatus.unknown
    assert hardware_telemetry.current_hardware_support_status(device) == HardwareTelemetrySupportStatus.supported
    device.hardware_health_status = "warning"
    device.hardware_telemetry_support_status = "supported"
    assert hardware_telemetry.current_hardware_health_status(device) == HardwareHealthStatus.unknown
    assert hardware_telemetry.current_hardware_support_status(device) == HardwareTelemetrySupportStatus.unknown

    device.device_type = DeviceType.emulator
    assert (
        hardware_telemetry.hardware_telemetry_state_for_device(device, settings=FakeSettingsReader({}))
        == HardwareTelemetryState.unsupported
    )
    device.device_type = DeviceType.real_device
    device.hardware_telemetry_support_status = HardwareTelemetrySupportStatus.unknown
    assert (
        hardware_telemetry.hardware_telemetry_state_for_device(device, settings=FakeSettingsReader({}))
        == HardwareTelemetryState.unknown
    )
    device.hardware_telemetry_support_status = HardwareTelemetrySupportStatus.unsupported
    assert (
        hardware_telemetry.hardware_telemetry_state_for_device(device, settings=FakeSettingsReader({}))
        == HardwareTelemetryState.unsupported
    )
    device.hardware_telemetry_support_status = HardwareTelemetrySupportStatus.supported
    device.hardware_telemetry_reported_at = datetime(2026, 5, 1, tzinfo=UTC)
    assert (
        hardware_telemetry.hardware_telemetry_state_for_device(
            device,
            now=datetime(2026, 5, 1, 0, 1, tzinfo=UTC),
            stale_timeout_sec=120,
            settings=FakeSettingsReader({}),
        )
        == HardwareTelemetryState.fresh
    )
    assert (
        hardware_telemetry.hardware_telemetry_state_for_device(
            device,
            now=datetime(2026, 5, 1, 1, 0, tzinfo=UTC),
            stale_timeout_sec=120,
            settings=FakeSettingsReader({}),
        )
        == HardwareTelemetryState.stale
    )

    _temp_settings = FakeSettingsReader(
        {"general.hardware_temperature_critical_c": 50, "general.hardware_temperature_warning_c": 40}
    )

    device.battery_temperature_c = 55
    device.hardware_telemetry_support_status = HardwareTelemetrySupportStatus.unknown
    assert (
        hardware_telemetry.derive_candidate_hardware_health_status(device, settings=_temp_settings)
        == HardwareHealthStatus.unknown
    )
    device.hardware_telemetry_support_status = HardwareTelemetrySupportStatus.supported
    assert (
        hardware_telemetry.derive_candidate_hardware_health_status(device, settings=_temp_settings)
        == HardwareHealthStatus.critical
    )
    device.battery_temperature_c = 45
    assert (
        hardware_telemetry.derive_candidate_hardware_health_status(device, settings=_temp_settings)
        == HardwareHealthStatus.warning
    )
    device.battery_temperature_c = None
    device.battery_level_percent = 80
    assert (
        hardware_telemetry.derive_candidate_hardware_health_status(device, settings=_temp_settings)
        == HardwareHealthStatus.healthy
    )
    device.battery_level_percent = None
    device.charging_state = HardwareChargingState.unknown
    assert (
        hardware_telemetry.derive_candidate_hardware_health_status(device, settings=_temp_settings)
        == HardwareHealthStatus.unknown
    )


async def test_apply_hardware_telemetry_sample_records_warning_transition() -> None:
    db = FlushSession()
    device = _telemetry_device()

    with (
        patch(
            "app.hosts.service_hardware_telemetry.control_plane_state_store.get_value", new=AsyncMock(return_value=None)
        ),
        patch("app.hosts.service_hardware_telemetry.control_plane_state_store.delete_value", new=AsyncMock()),
        patch("app.hosts.service_hardware_telemetry.record_event", new=AsyncMock()) as record_event,
    ):
        publisher = Mock()
        svc = HardwareTelemetryService(
            publisher=publisher,
            settings=FakeSettingsReader(
                {
                    "general.hardware_temperature_critical_c": 50,
                    "general.hardware_temperature_warning_c": 40,
                    "general.hardware_telemetry_consecutive_samples": 1,
                }
            ),
            circuit_breaker=Mock(),
        )
        status = await svc.apply_telemetry_sample(
            db,
            device,
            {
                "battery_level_percent": "80",
                "battery_temperature_c": "45.5",
                "charging_state": "charging",
                "support_status": "supported",
                "reported_at": "2026-05-01T12:00:00Z",
            },
        )

    assert status == HardwareHealthStatus.warning
    assert db.flushed is True
    assert device.battery_level_percent == 80
    assert device.battery_temperature_c == 45.5
    assert device.charging_state == HardwareChargingState.charging
    record_event.assert_awaited_once()
    publisher.queue_for_session.assert_called_once()


async def test_effective_hardware_health_requires_consecutive_samples() -> None:
    db = object()
    device = _telemetry_device(hardware_health_status=HardwareHealthStatus.healthy)

    with (
        patch(
            "app.hosts.service_hardware_telemetry.control_plane_state_store.get_value", new=AsyncMock(return_value=None)
        ),
        patch("app.hosts.service_hardware_telemetry.control_plane_state_store.set_value", new=AsyncMock()) as set_value,
        patch(
            "app.hosts.service_hardware_telemetry.control_plane_state_store.delete_value", new=AsyncMock()
        ) as delete_value,
    ):
        status = await hardware_telemetry._resolve_effective_hardware_health_status(
            db,
            device,
            HardwareHealthStatus.critical,
            settings=FakeSettingsReader({"general.hardware_telemetry_consecutive_samples": 2}),
        )

    assert status == HardwareHealthStatus.healthy
    set_value.assert_awaited_once()
    delete_value.assert_not_awaited()

    with patch(
        "app.hosts.service_hardware_telemetry.control_plane_state_store.delete_value",
        new=AsyncMock(),
    ) as delete_value:
        status = await hardware_telemetry._resolve_effective_hardware_health_status(
            db,
            device,
            HardwareHealthStatus.healthy,
            settings=FakeSettingsReader({"general.hardware_telemetry_consecutive_samples": 2}),
        )
    assert status == HardwareHealthStatus.healthy
    delete_value.assert_awaited_once()

    device.hardware_health_status = HardwareHealthStatus.critical
    with patch(
        "app.hosts.service_hardware_telemetry.control_plane_state_store.delete_value",
        new=AsyncMock(),
    ) as delete_value:
        status = await hardware_telemetry._resolve_effective_hardware_health_status(
            db,
            device,
            HardwareHealthStatus.warning,
            settings=FakeSettingsReader({"general.hardware_telemetry_consecutive_samples": 2}),
        )
    assert status == HardwareHealthStatus.warning
    delete_value.assert_awaited_once()


async def test_get_device_telemetry_handles_missing_host_and_agent_errors() -> None:
    svc = HardwareTelemetryService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
    )
    assert await svc._get_device_telemetry(_telemetry_device(host=None)) is None
    host = SimpleNamespace(ip="10.0.0.1", agent_port=5100)
    device = _telemetry_device(host=host)
    with patch(
        "app.hosts.service_hardware_telemetry.fetch_pack_device_telemetry",
        new=AsyncMock(side_effect=AgentCallError("10.0.0.1", "failed")),
    ):
        assert await svc._get_device_telemetry(device) is None
    with patch(
        "app.hosts.service_hardware_telemetry.fetch_pack_device_telemetry",
        new=AsyncMock(return_value={"battery_level_percent": 80}),
    ) as fetch:
        assert await svc._get_device_telemetry(device) == {"battery_level_percent": 80}
    fetch.assert_awaited_once()


async def test_poll_hardware_telemetry_commits_samples_and_rolls_back_failures() -> None:
    devices = [_telemetry_device(), _telemetry_device(), _telemetry_device()]

    class Result:
        def scalars(self) -> Result:
            return self

        def all(self) -> list[object]:
            return devices

    class PollSession(FlushSession):
        async def execute(self, *_args: object, **_kwargs: object) -> Result:
            return Result()

    db = PollSession()
    svc = HardwareTelemetryService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
    )
    with (
        patch.object(
            svc,
            "_get_device_telemetry",
            new=AsyncMock(side_effect=[None, {"battery_level_percent": 80}, {"battery_level_percent": 70}]),
        ),
        patch.object(
            svc,
            "apply_telemetry_sample",
            new=AsyncMock(side_effect=[HardwareHealthStatus.healthy, RuntimeError("boom")]),
        ),
    ):
        await svc.poll_once(db)

    assert db.committed is True
    assert db.rolled_back is True


async def test_host_resource_sample_coercion_and_apply() -> None:
    db = FlushSession()
    host = SimpleNamespace(id=uuid.uuid4())

    assert host_resource_telemetry._coerce_int(True) is None
    assert host_resource_telemetry._coerce_int(Decimal("12.6")) == 13
    assert host_resource_telemetry._coerce_float(False) is None
    assert host_resource_telemetry._coerce_float(Decimal("12.5")) == 12.5
    assert host_resource_telemetry._window_exceeds_retention(
        since=datetime(2026, 5, 1, tzinfo=UTC),
        until=datetime(2026, 5, 3, tzinfo=UTC),
        retention_hours=24,
    )
    sample = host_resource_telemetry._sample_from_row(
        (datetime(2026, 5, 1, tzinfo=UTC), Decimal("12.5"), Decimal("10.6"), 20, 1, 2, 50)
    )
    assert sample.memory_used_mb == 11

    row = await HostResourceTelemetryService(
        settings=FakeSettingsReader({}), circuit_breaker=Mock()
    ).apply_host_resource_sample(
        db,
        host,
        {
            "recorded_at": "2026-05-01T12:00:00Z",
            "cpu_percent": 12.5,
            "memory_used_mb": 1024.2,
            "memory_total_mb": 2048,
            "disk_used_gb": Decimal("10.5"),
            "disk_total_gb": 100,
            "disk_percent": 10,
        },
    )

    assert db.flushed is True
    assert row.host_id == host.id
    assert row.cpu_percent == 12.5
    assert row.memory_used_mb == 1024


async def test_poll_host_resource_telemetry_handles_agent_and_unexpected_errors() -> None:
    host = SimpleNamespace(id=uuid.uuid4(), hostname="host-1", ip="10.0.0.1", agent_port=5100)

    class Result:
        def scalars(self) -> Result:
            return self

        def all(self) -> list[object]:
            return [host, host, host]

    class PollSession(FlushSession):
        async def execute(self, *_args: object, **_kwargs: object) -> Result:
            return Result()

    db = PollSession()
    with patch(
        "app.hosts.service_resource_telemetry.agent_host_telemetry",
        new=AsyncMock(side_effect=[None, AgentCallError("10.0.0.1", "failed"), RuntimeError("boom")]),
    ):
        await HostResourceTelemetryService(settings=FakeSettingsReader({}), circuit_breaker=Mock()).poll_once(db)

    assert db.rolled_back is True


async def test_poll_host_resource_telemetry_commits_successful_samples() -> None:
    host = SimpleNamespace(id=uuid.uuid4(), hostname="host-1", ip="10.0.0.1", agent_port=5100)

    class Result:
        def scalars(self) -> Result:
            return self

        def all(self) -> list[object]:
            return [host]

    class PollSession(FlushSession):
        async def execute(self, *_args: object, **_kwargs: object) -> Result:
            return Result()

    db = PollSession()
    with patch(
        "app.hosts.service_resource_telemetry.agent_host_telemetry",
        new=AsyncMock(return_value={"cpu_percent": 50}),
    ):
        await HostResourceTelemetryService(settings=FakeSettingsReader({}), circuit_breaker=Mock()).poll_once(db)

    assert db.committed is True
    assert db.added


async def test_fetch_host_resource_telemetry_validation_paths() -> None:
    host_id = uuid.uuid4()

    class FetchSession:
        async def scalar(self, *_args: object, **_kwargs: object) -> object | None:
            return host_id

    svc = HostResourceTelemetryService(
        settings=FakeSettingsReader({"retention.host_resource_telemetry_hours": 24}),
        circuit_breaker=Mock(),
    )
    for since, until, bucket_minutes, message in (
        (
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 5, 1, tzinfo=UTC),
            5,
            "since must be earlier",
        ),
        (
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 5, 2, tzinfo=UTC),
            0,
            "bucket_minutes",
        ),
        (
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 5, 3, tzinfo=UTC),
            5,
            "retention",
        ),
    ):
        with pytest.raises(ValueError) as exc:
            await svc.fetch_host_resource_telemetry(
                FetchSession(),  # type: ignore[arg-type]
                host_id,
                since=since,
                until=until,
                bucket_minutes=bucket_minutes,
            )
        assert message in str(exc.value)


async def test_fetch_host_resource_telemetry_returns_none_for_missing_host() -> None:
    class MissingHostSession:
        async def scalar(self, *_args: object, **_kwargs: object) -> object | None:
            return None

    assert (
        await HostResourceTelemetryService(
            settings=FakeSettingsReader({}), circuit_breaker=Mock()
        ).fetch_host_resource_telemetry(
            MissingHostSession(),  # type: ignore[arg-type]
            uuid.uuid4(),
            since=datetime(2026, 5, 1, tzinfo=UTC),
            until=datetime(2026, 5, 2, tzinfo=UTC),
            bucket_minutes=5,
        )
        is None
    )
