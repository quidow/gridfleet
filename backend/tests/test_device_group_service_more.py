from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from app.models.device_group import DeviceGroup, GroupType
from app.schemas.device_filters import DeviceGroupFilters
from app.schemas.device_group import DeviceGroupCreate, DeviceGroupUpdate
from app.services import device_group_service
from tests.helpers import create_device_record, seed_host_and_device, settle_after_commit_tasks

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def test_static_group_membership_counts_and_idempotent_changes(db_session: AsyncSession) -> None:
    host, first_device = await seed_host_and_device(db_session, identity="group-static-1")
    second_device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="group-static-2",
        name="Group Static 2",
    )
    group = await device_group_service.create_group(
        db_session,
        DeviceGroupCreate(name="static phones", description="operator set", group_type="static"),
    )
    await settle_after_commit_tasks()

    assert await device_group_service.add_members(db_session, group.id, [first_device.id, second_device.id]) == 2
    assert await device_group_service.add_members(db_session, group.id, [first_device.id]) == 0

    groups = await device_group_service.list_groups(db_session)
    assert groups[0]["device_count"] == 2

    detail = await device_group_service.get_group(db_session, group.id)
    assert detail is not None
    assert [device.id for device in detail["devices"]] == [first_device.id, second_device.id]
    assert await device_group_service.get_group_device_ids(db_session, group.id) == [first_device.id, second_device.id]

    assert await device_group_service.remove_members(db_session, group.id, [first_device.id]) == 1
    assert await device_group_service.remove_members(db_session, group.id, [first_device.id]) == 0

    updated = await device_group_service.update_group(
        db_session,
        group.id,
        DeviceGroupUpdate(name="static phones updated", description="renamed"),
    )
    assert updated is not None
    assert updated.name == "static phones updated"
    assert updated.description == "renamed"

    assert await device_group_service.delete_group(db_session, group.id) is True
    assert await device_group_service.delete_group(db_session, group.id) is False
    assert await device_group_service.get_group(db_session, group.id) is None
    assert await device_group_service.update_group(db_session, group.id, DeviceGroupUpdate(name="missing")) is None


async def test_dynamic_group_resolves_and_counts_via_device_filters(db_session: AsyncSession) -> None:
    _host, device = await seed_host_and_device(db_session, identity="group-dynamic-1")
    filters = DeviceGroupFilters(platform_id="android_mobile", tags={"tier": "smoke"})
    group = await device_group_service.create_group(
        db_session,
        DeviceGroupCreate(name="dynamic smoke", group_type="dynamic", filters=filters),
    )
    await settle_after_commit_tasks()

    with (
        patch(
            "app.services.device_group_service.device_service.count_devices_by_filters",
            new=AsyncMock(return_value=5),
        ) as count_devices,
        patch(
            "app.services.device_group_service.device_service.list_devices_by_filters",
            new=AsyncMock(return_value=[device]),
        ) as list_devices,
    ):
        groups = await device_group_service.list_groups(db_session)
        detail = await device_group_service.get_group(db_session, group.id)
        device_ids = await device_group_service.get_group_device_ids(db_session, group.id)

    assert groups[0]["group_type"] == "dynamic"
    assert groups[0]["filters"] == {"platform_id": "android_mobile", "tags": {"tier": "smoke"}}
    assert groups[0]["device_count"] == 5
    assert detail is not None
    assert detail["device_count"] == 1
    assert detail["devices"] == [device]
    assert device_ids == [device.id]
    count_devices.assert_awaited_once()
    assert list_devices.await_count == 2

    updated = await device_group_service.update_group(
        db_session,
        group.id,
        DeviceGroupUpdate(filters=DeviceGroupFilters(platform_id="ios")),
    )
    assert updated is not None
    assert updated.filters == {"platform_id": "ios"}
    with patch(
        "app.services.device_group_service.device_service.list_devices_by_filters",
        new=AsyncMock(return_value=[device]),
    ):
        assert await device_group_service.get_group_device_ids(db_session, group.id) == [device.id]


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

    assert await device_group_service.get_group_device_ids(db_session, group.id) == []
