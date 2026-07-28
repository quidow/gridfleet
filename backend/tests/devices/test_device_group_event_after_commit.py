"""Contract tests for device group event queueing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate
from app.devices.services.groups import DeviceGroupsService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from tests.fakes import FakeSettingsReader
from tests.helpers import dispatch_committed_events, seed_host_and_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _svc() -> DeviceGroupsService:
    _settings = FakeSettingsReader({})
    return DeviceGroupsService(
        publisher=event_bus,
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
    )


async def test_create_group_queues_updated(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    group = await _svc().create_group(
        db_session,
        DeviceGroupCreate(key="contract", name="contract", description=None),
    )
    # create_group no longer owns the commit (Phase 11); the test is the caller
    # here, so it owns the boundary the way the router does.
    await db_session.commit()
    await dispatch_committed_events()

    events = [p for n, p in event_bus_capture if n == "device_group.updated"]
    assert len(events) == 1
    assert events[0]["action"] == "created"
    assert events[0]["group_key"] == group.group_key


async def test_update_group_queues_updated(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    svc = _svc()
    group = await svc.create_group(
        db_session,
        DeviceGroupCreate(key="update-me", name="update-me", description=None),
    )
    # create_group no longer commits (Phase 11): commit here so the "created"
    # event is delivered and cleared before the update, rather than riding
    # update_group's own commit and doubling up below.
    await db_session.commit()
    await dispatch_committed_events()
    event_bus_capture.clear()

    updated = await svc.update_group(db_session, group.group_key, DeviceGroupUpdate(name="updated-name"))
    assert updated is not None
    assert db_session.in_transaction(), "update_group must leave event publication for the caller's commit"
    await db_session.commit()
    await dispatch_committed_events()

    assert [payload for name, payload in event_bus_capture if name == "device_group.updated"] == [
        {"group_key": "update-me", "action": "updated"}
    ]


async def test_delete_group_queues_updated_deleted(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    svc = _svc()
    group = await svc.create_group(
        db_session,
        DeviceGroupCreate(key="to-delete", name="to-delete", description=None),
    )
    await db_session.commit()
    await dispatch_committed_events()
    event_bus_capture.clear()

    deleted = await svc.delete_group(db_session, group.group_key)
    assert deleted
    assert db_session.in_transaction(), "delete_group must leave event publication for the caller's commit"
    await db_session.commit()
    await dispatch_committed_events()

    events = [p for n, p in event_bus_capture if n == "device_group.updated"]
    assert any(p["action"] == "deleted" for p in events)


async def test_add_members_queues_members_changed(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    svc = _svc()
    group = await svc.create_group(db_session, DeviceGroupCreate(key="add-members", name="add-members"))
    _, device = await seed_host_and_device(db_session, identity="group-add-1")
    await dispatch_committed_events()
    event_bus_capture.clear()

    await svc.add_members(db_session, group.group_key, [device.id])
    await db_session.commit()
    await dispatch_committed_events()

    events = [p for n, p in event_bus_capture if n == "device_group.members_changed"]
    assert len(events) == 1
    assert events[0]["added"] == 1


async def test_remove_members_queues_members_changed(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    svc = _svc()
    group = await svc.create_group(db_session, DeviceGroupCreate(key="remove-members", name="remove-members"))
    _, device = await seed_host_and_device(db_session, identity="group-remove-1")
    await svc.add_members(db_session, group.group_key, [device.id])
    await db_session.commit()
    await dispatch_committed_events()
    event_bus_capture.clear()

    await svc.remove_members(db_session, group.group_key, [device.id])
    await db_session.commit()
    await dispatch_committed_events()

    events = [p for n, p in event_bus_capture if n == "device_group.members_changed"]
    assert len(events) == 1
    assert events[0]["removed"] == 1
