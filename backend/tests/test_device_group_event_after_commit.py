"""Contract tests for device group event queueing."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate
from app.devices.services import groups as device_group_service
from tests.helpers import seed_host_and_device, settle_after_commit_tasks

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_create_group_queues_updated(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    group = await device_group_service.create_group(
        db_session,
        DeviceGroupCreate(name="contract", description=None),
    )
    await settle_after_commit_tasks()

    events = [p for n, p in event_bus_capture if n == "device_group.updated"]
    assert len(events) == 1
    assert events[0]["action"] == "created"
    assert events[0]["group_id"] == str(group.id)


async def test_update_group_queues_updated(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    group = await device_group_service.create_group(
        db_session,
        DeviceGroupCreate(name="update-me", description=None),
    )
    event_bus_capture.clear()

    await device_group_service.update_group(
        db_session,
        group.id,
        DeviceGroupUpdate(name="updated-name"),
    )
    await settle_after_commit_tasks()

    events = [p for n, p in event_bus_capture if n == "device_group.updated"]
    assert len(events) == 1
    assert events[0]["action"] == "updated"


async def test_delete_group_queues_updated_deleted(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    group = await device_group_service.create_group(
        db_session,
        DeviceGroupCreate(name="to-delete", description=None),
    )
    event_bus_capture.clear()

    await device_group_service.delete_group(db_session, group.id)
    await settle_after_commit_tasks()

    events = [p for n, p in event_bus_capture if n == "device_group.updated"]
    assert any(p["action"] == "deleted" for p in events)


async def test_add_members_queues_members_changed(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    group = await device_group_service.create_group(db_session, DeviceGroupCreate(name="add-members"))
    _, device = await seed_host_and_device(db_session, identity="group-add-1")
    event_bus_capture.clear()

    await device_group_service.add_members(db_session, group.id, [device.id])
    await settle_after_commit_tasks()

    events = [p for n, p in event_bus_capture if n == "device_group.members_changed"]
    assert len(events) == 1
    assert events[0]["added"] == 1


async def test_remove_members_queues_members_changed(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    group = await device_group_service.create_group(db_session, DeviceGroupCreate(name="remove-members"))
    _, device = await seed_host_and_device(db_session, identity="group-remove-1")
    await device_group_service.add_members(db_session, group.id, [device.id])
    event_bus_capture.clear()

    await device_group_service.remove_members(db_session, group.id, [device.id])
    await settle_after_commit_tasks()

    events = [p for n, p in event_bus_capture if n == "device_group.members_changed"]
    assert len(events) == 1
    assert events[0]["removed"] == 1
