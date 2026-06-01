from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from app.devices.models import DeviceGroup, GroupType
from app.devices.schemas.filters import DeviceGroupFilters
from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate
from app.devices.services import groups as device_group_service
from app.devices.services.groups import DeviceGroupsService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device_record, seed_host_and_device, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _svc(settings: object | None = None) -> DeviceGroupsService:
    _settings = settings or FakeSettingsReader({})
    return DeviceGroupsService(
        publisher=event_bus,
        settings=_settings,
        crud=DeviceCrudService(settings=_settings, identity=DeviceIdentityConflictService(), publisher=event_bus),
    )


async def test_static_group_membership_counts_and_idempotent_changes(db_session: AsyncSession) -> None:
    host, first_device = await seed_host_and_device(db_session, identity="group-static-1")
    second_device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="group-static-2",
        name="Group Static 2",
    )
    svc = _svc()
    group = await svc.create_group(
        db_session,
        DeviceGroupCreate(name="static phones", description="operator set", group_type="static"),
    )
    await settle_after_commit_tasks()

    assert await svc.add_members(db_session, group.id, [first_device.id, second_device.id]) == 2
    assert await svc.add_members(db_session, group.id, [first_device.id]) == 0

    groups = await svc.list_groups(db_session)
    assert groups[0]["device_count"] == 2

    detail = await svc.get_group(db_session, group.id)
    assert detail is not None
    assert [device.id for device in detail["devices"]] == [first_device.id, second_device.id]
    assert await svc.get_group_device_ids(db_session, group.id) == [first_device.id, second_device.id]

    assert await svc.remove_members(db_session, group.id, [first_device.id]) == 1
    assert await svc.remove_members(db_session, group.id, [first_device.id]) == 0

    updated = await svc.update_group(
        db_session,
        group.id,
        DeviceGroupUpdate(name="static phones updated", description="renamed"),
    )
    assert updated is not None
    assert updated.name == "static phones updated"
    assert updated.description == "renamed"

    assert await svc.delete_group(db_session, group.id) is True
    assert await svc.delete_group(db_session, group.id) is False
    assert await svc.get_group(db_session, group.id) is None
    assert await svc.update_group(db_session, group.id, DeviceGroupUpdate(name="missing")) is None


async def test_dynamic_group_resolves_and_counts_via_device_filters(db_session: AsyncSession) -> None:
    _host, device = await seed_host_and_device(db_session, identity="group-dynamic-1")
    filters = DeviceGroupFilters(platform_id="android_mobile", tags={"tier": "smoke"})
    svc = _svc()
    group = await svc.create_group(
        db_session,
        DeviceGroupCreate(name="dynamic smoke", group_type="dynamic", filters=filters),
    )
    await settle_after_commit_tasks()

    mock_crud = AsyncMock()
    mock_crud.count_devices_by_filters = AsyncMock(return_value=5)
    mock_crud.list_devices_by_filters = AsyncMock(return_value=[device])
    svc_mocked = DeviceGroupsService(publisher=event_bus, settings=FakeSettingsReader({}), crud=mock_crud)
    groups = await svc_mocked.list_groups(db_session)
    detail = await svc_mocked.get_group(db_session, group.id)
    device_ids = await svc_mocked.get_group_device_ids(db_session, group.id)

    assert groups[0]["group_type"] == "dynamic"
    assert groups[0]["filters"] == {"platform_id": "android_mobile", "tags": {"tier": "smoke"}}
    assert groups[0]["device_count"] == 5
    assert detail is not None
    assert detail["device_count"] == 1
    assert detail["devices"] == [device]
    assert device_ids == [device.id]
    mock_crud.count_devices_by_filters.assert_awaited_once()
    assert mock_crud.list_devices_by_filters.await_count == 2

    updated = await svc.update_group(
        db_session,
        group.id,
        DeviceGroupUpdate(filters=DeviceGroupFilters(platform_id="ios")),
    )
    assert updated is not None
    assert updated.filters == {"platform_id": "ios"}
    mock_crud2 = AsyncMock()
    mock_crud2.list_devices_by_filters = AsyncMock(return_value=[device])
    svc_mocked2 = DeviceGroupsService(publisher=event_bus, settings=FakeSettingsReader({}), crud=mock_crud2)
    assert await svc_mocked2.get_group_device_ids(db_session, group.id) == [device.id]


async def test_filter_serialization_helpers_round_trip_valid_payloads() -> None:
    assert device_group_service._dump_filters(None) is None
    assert device_group_service._serialize_filters(None) is None

    filters = DeviceGroupFilters(platform_id="roku_network", connection_type="network")
    dumped = device_group_service._dump_filters(filters)
    assert dumped == {"platform_id": "roku_network", "connection_type": "network"}
    assert device_group_service._serialize_filters(dumped) == dumped


async def test_get_group_device_ids_returns_empty_for_missing_group(db_session: AsyncSession) -> None:
    group = DeviceGroup(name="orphan", group_type=GroupType.static, filters=None)
    db_session.add(group)
    await db_session.commit()
    await db_session.delete(group)
    await db_session.commit()

    assert await _svc().get_group_device_ids(db_session, group.id) == []
