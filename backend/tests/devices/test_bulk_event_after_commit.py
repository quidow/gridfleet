"""Contract tests for queued same-session bulk operation summary events."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from app.devices.services.bulk import BulkOperationsService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.maintenance import MaintenanceService
from app.devices.services.service import DeviceCrudService
from app.lifecycle.services.operator_node import OperatorNodeLifecycleService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import seed_host_and_device, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _svc(*, maintenance: object | None = None) -> BulkOperationsService:
    _settings = FakeSettingsReader()
    return BulkOperationsService(
        publisher=event_bus,
        settings=_settings,
        circuit_breaker=MagicMock(),
        maintenance=maintenance
        or MaintenanceService(review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus),
        crud=DeviceCrudService(settings=_settings, identity=DeviceIdentityConflictService(), publisher=event_bus),
        operator=OperatorNodeLifecycleService(review=build_review_service(), settings=_settings, publisher=event_bus),
    )


async def test_bulk_enter_maintenance_queues_summary(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="bulk-enter-maint-1")
    event_bus_capture.clear()

    await _svc().bulk_enter_maintenance(db_session, [device.id])
    await settle_after_commit_tasks()

    summary = [p for n, p in event_bus_capture if n == "bulk.operation_completed"]
    assert len(summary) == 1
    assert summary[0]["operation"] == "enter_maintenance"


async def test_bulk_exit_maintenance_queues_summary(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="bulk-exit-maint-1")
    await _svc().bulk_enter_maintenance(db_session, [device.id])
    event_bus_capture.clear()

    await _svc().bulk_exit_maintenance(db_session, [device.id])
    await settle_after_commit_tasks()

    summary = [p for n, p in event_bus_capture if n == "bulk.operation_completed"]
    assert len(summary) == 1
    assert summary[0]["operation"] == "exit_maintenance"
