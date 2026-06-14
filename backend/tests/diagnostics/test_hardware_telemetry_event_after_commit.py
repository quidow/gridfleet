"""Contract tests for hardware telemetry event queueing."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.hosts.service_hardware_telemetry import HardwareTelemetryService
from tests.fakes import FakeSettingsReader
from tests.helpers import seed_host_and_device, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_hardware_health_changed_queues_after_commit(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, device = await seed_host_and_device(db_session, identity="hardware-warning-1")
    event_bus_capture.clear()
    _hw_settings = FakeSettingsReader(
        {"general.hardware_telemetry_consecutive_samples": 1, "general.hardware_temperature_critical_c": 40}
    )
    _hw_svc = HardwareTelemetryService(publisher=event_bus, settings=_hw_settings, circuit_breaker=Mock())

    await _hw_svc.apply_telemetry_sample(
        db_session,
        device,
        {
            "support_status": "supported",
            "battery_level_percent": 80,
            "battery_temperature_c": 50,
        },
    )
    await settle_after_commit_tasks()
    assert event_bus_capture == []

    await db_session.commit()
    await settle_after_commit_tasks()

    changed = [p for n, p in event_bus_capture if n == "device.hardware_health_changed"]
    assert len(changed) == 1
    assert changed[0]["device_id"] == str(device.id)
    assert changed[0]["new_status"] in {"warning", "critical"}


async def test_hardware_health_changed_dropped_on_rollback(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, device = await seed_host_and_device(db_session, identity="hardware-rollback-1")
    event_bus_capture.clear()
    _rollback_settings = FakeSettingsReader(
        {"general.hardware_telemetry_consecutive_samples": 1, "general.hardware_temperature_critical_c": 40}
    )
    _rollback_svc = HardwareTelemetryService(publisher=event_bus, settings=_rollback_settings, circuit_breaker=Mock())

    await _rollback_svc.apply_telemetry_sample(
        db_session,
        device,
        {
            "support_status": "supported",
            "battery_level_percent": 80,
            "battery_temperature_c": 50,
        },
    )
    await db_session.rollback()
    await settle_after_commit_tasks()

    assert [n for n, _ in event_bus_capture if n == "device.hardware_health_changed"] == []
