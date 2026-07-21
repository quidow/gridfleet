from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

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
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
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
        DeviceGroupCreate(key="static-phones", name="static phones", description="operator set", group_type="static"),
    )
    await settle_after_commit_tasks()

    assert await svc.add_members(db_session, group["key"], [first_device.id, second_device.id]) == 2
    assert await svc.add_members(db_session, group["key"], [first_device.id]) == 0

    groups = await svc.list_groups(db_session)
    assert groups[0]["device_count"] == 2

    detail = await svc.get_group(db_session, group["key"])
    assert detail is not None
    assert [device.id for device in detail["devices"]] == [first_device.id, second_device.id]
    assert await svc.get_group_device_ids(db_session, group["key"]) == [first_device.id, second_device.id]

    assert await svc.remove_members(db_session, group["key"], [first_device.id]) == 1
    assert await svc.remove_members(db_session, group["key"], [first_device.id]) == 0

    updated = await svc.update_group(
        db_session,
        group["key"],
        DeviceGroupUpdate(name="static phones updated", description="renamed"),
    )
    assert updated is not None
    assert updated.name == "static phones updated"
    assert updated.description == "renamed"

    assert await svc.delete_group(db_session, group["key"]) is True
    assert await svc.delete_group(db_session, group["key"]) is False
    assert await svc.get_group(db_session, group["key"]) is None
    assert await svc.update_group(db_session, group["key"], DeviceGroupUpdate(name="missing")) is None


async def test_dynamic_group_resolves_and_counts_via_device_filters(db_session: AsyncSession) -> None:
    _host, device = await seed_host_and_device(db_session, identity="group-dynamic-1")
    svc = _svc()
    # Classification now lives in a static group the dynamic filter references.
    await svc.create_group(
        db_session,
        DeviceGroupCreate(key="tier-smoke", name="tier smoke", group_type="static"),
    )
    await svc.add_members(db_session, "tier-smoke", [device.id])
    filters = DeviceGroupFilters(platform_id="android_mobile", member_of=["tier-smoke"])
    group = await svc.create_group(
        db_session,
        DeviceGroupCreate(key="dynamic-smoke", name="dynamic smoke", group_type="dynamic", filters=filters),
    )
    await settle_after_commit_tasks()

    groups = await svc.list_groups(db_session)
    detail = await svc.get_group(db_session, group["key"])
    device_ids = await svc.get_group_device_ids(db_session, group["key"])

    assert groups[0]["group_type"] == "dynamic"
    assert groups[0]["filters"] == {"platform_id": "android_mobile", "member_of": ["tier-smoke"]}
    assert groups[0]["device_count"] == 1
    assert detail is not None
    assert detail["device_count"] == 1
    assert [d.id for d in detail["devices"]] == [device.id]
    assert device_ids == [device.id]

    updated = await svc.update_group(
        db_session,
        group["key"],
        DeviceGroupUpdate(filters=DeviceGroupFilters(platform_id="ios")),
    )
    assert updated is not None
    assert updated.filters == {"platform_id": "ios"}
    # No iOS device is seeded, so membership is empty after the filter change.
    assert await svc.get_group_device_ids(db_session, group["key"]) == []


async def test_filter_serialization_helpers_round_trip_valid_payloads() -> None:
    assert device_group_service._dump_filters(None) is None
    assert device_group_service._serialize_filters(None) is None

    filters = DeviceGroupFilters(platform_id="roku_network", connection_type="network")
    dumped = device_group_service._dump_filters(filters)
    assert dumped == {"platform_id": "roku_network", "connection_type": "network"}
    assert device_group_service._serialize_filters(dumped) == dumped


async def test_get_group_device_ids_returns_empty_for_missing_group(db_session: AsyncSession) -> None:
    group = DeviceGroup(key="orphan", name="orphan", group_type=GroupType.static, filters=None)
    db_session.add(group)
    await db_session.commit()
    await db_session.delete(group)
    await db_session.commit()

    assert await _svc().get_group_device_ids(db_session, group.key) == []


async def test_delete_dynamic_group_succeeds_when_unreferenced(db_session: AsyncSession) -> None:
    """The reference scan runs for every group type and must not over-reject.

    ``delete_group`` no longer gates ``_assert_no_references`` on the target being
    static, so a dynamic group with no dependents must still delete cleanly.
    """
    svc = _svc()
    await svc.create_group(
        db_session,
        DeviceGroupCreate(key="del-static", name="del static", group_type="static"),
    )
    dynamic = await svc.create_group(
        db_session,
        DeviceGroupCreate(
            key="del-dynamic",
            name="del dynamic",
            group_type="dynamic",
            filters=DeviceGroupFilters(member_of=["del-static"]),
        ),
    )
    await settle_after_commit_tasks()

    assert await svc.delete_group(db_session, dynamic["key"]) is True
    assert await svc.get_group(db_session, dynamic["key"]) is None


async def test_delete_dynamic_group_rejects_dangling_reference(db_session: AsyncSession) -> None:
    """A ``member_of`` naming a dynamic group is unreachable through the API
    (``_assert_member_of_resolves`` rejects it), but nothing structural stops the
    row from existing — a hand-written row or a data migration can mint one. The
    delete path scans references unconditionally so such a row cannot be orphaned.
    """
    svc = _svc()
    target = await svc.create_group(
        db_session,
        DeviceGroupCreate(key="dangling-target", name="dangling target", group_type="dynamic"),
    )
    await settle_after_commit_tasks()
    # Bypass the service so the otherwise-rejected reference reaches the table.
    db_session.add(
        DeviceGroup(
            key="dangling-ref",
            name="dangling ref",
            group_type=GroupType.dynamic,
            filters={"member_of": [target["key"]]},
        )
    )
    await db_session.commit()

    with pytest.raises(device_group_service.GroupReferencedError) as exc:
        await svc.delete_group(db_session, target["key"])
    assert exc.value.dependents == ["dangling-ref"]
