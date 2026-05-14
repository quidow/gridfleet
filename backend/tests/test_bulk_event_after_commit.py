"""Contract tests for queued same-session bulk operation summary events."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.devices.services import bulk as bulk_service
from tests.helpers import seed_host_and_device, settle_after_commit_tasks

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_bulk_set_auto_manage_queues_summary(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="bulk-auto-1")
    event_bus_capture.clear()

    await bulk_service.bulk_set_auto_manage(db_session, [device.id], auto_manage=False)
    await settle_after_commit_tasks()

    summary = [p for n, p in event_bus_capture if n == "bulk.operation_completed"]
    assert len(summary) == 1
    assert summary[0]["operation"] == "set_auto_manage"


async def test_bulk_update_tags_queues_summary(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="bulk-tags-1")
    event_bus_capture.clear()

    await bulk_service.bulk_update_tags(db_session, [device.id], {"suite": "contract"})
    await settle_after_commit_tasks()

    summary = [p for n, p in event_bus_capture if n == "bulk.operation_completed"]
    assert len(summary) == 1
    assert summary[0]["operation"] == "update_tags"


async def test_bulk_enter_maintenance_queues_summary(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="bulk-enter-maint-1")
    event_bus_capture.clear()

    await bulk_service.bulk_enter_maintenance(db_session, [device.id])
    await settle_after_commit_tasks()

    summary = [p for n, p in event_bus_capture if n == "bulk.operation_completed"]
    assert len(summary) == 1
    assert summary[0]["operation"] == "enter_maintenance"


async def test_bulk_exit_maintenance_queues_summary(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="bulk-exit-maint-1")
    await bulk_service.bulk_enter_maintenance(db_session, [device.id])
    event_bus_capture.clear()

    await bulk_service.bulk_exit_maintenance(db_session, [device.id])
    await settle_after_commit_tasks()

    summary = [p for n, p in event_bus_capture if n == "bulk.operation_completed"]
    assert len(summary) == 1
    assert summary[0]["operation"] == "exit_maintenance"
