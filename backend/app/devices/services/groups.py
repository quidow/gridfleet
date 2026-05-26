from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload

from app.devices.models import Device, DeviceGroup, DeviceGroupMembership, GroupType
from app.devices.schemas.filters import DeviceGroupFilters, DeviceQueryFilters
from app.devices.services import service as device_service
from app.events import event_bus as _default_event_bus
from app.events import queue_event_for_session

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate
    from app.events.event_bus import EventBus


async def create_group(db: AsyncSession, data: DeviceGroupCreate, *, publisher: EventBus | None = None) -> DeviceGroup:
    group = DeviceGroup(
        name=data.name,
        description=data.description,
        group_type=GroupType(data.group_type),
        filters=_dump_filters(data.filters),
    )
    db.add(group)
    await db.flush()
    queue_event_for_session(
        db,
        "device_group.updated",
        {"group_id": str(group.id), "action": "created"},
        publisher=publisher or _default_event_bus,
    )
    await db.commit()
    await db.refresh(group)
    return group


async def list_groups(db: AsyncSession) -> list[dict[str, Any]]:
    stmt = select(DeviceGroup).order_by(DeviceGroup.name)
    result = await db.execute(stmt)
    groups = list(result.scalars().all())

    static_group_ids = [group.id for group in groups if group.group_type == GroupType.static]
    static_counts: dict[uuid.UUID, int] = {}
    if static_group_ids:
        count_stmt = (
            select(DeviceGroupMembership.group_id, func.count(DeviceGroupMembership.device_id))
            .where(DeviceGroupMembership.group_id.in_(static_group_ids))
            .group_by(DeviceGroupMembership.group_id)
        )
        count_result = await db.execute(count_stmt)
        static_counts = {group_id: int(count or 0) for group_id, count in count_result.all()}

    output = []
    for group in groups:
        if group.group_type == GroupType.dynamic:
            count = await _count_dynamic_members(db, group.filters or {})
        else:
            count = static_counts.get(group.id, 0)

        output.append(_serialize_group(group, device_count=count))
    return output


async def get_group(db: AsyncSession, group_id: uuid.UUID) -> dict[str, Any] | None:
    stmt = (
        select(DeviceGroup)
        .where(DeviceGroup.id == group_id)
        .options(selectinload(DeviceGroup.memberships).selectinload(DeviceGroupMembership.device))
    )
    result = await db.execute(stmt)
    group = result.scalar_one_or_none()
    if group is None:
        return None

    if group.group_type == GroupType.dynamic:
        devices = await _resolve_dynamic_members(db, group.filters or {})
    else:
        devices = [m.device for m in group.memberships if m.device is not None]

    return {
        **_serialize_group(group, device_count=len(devices)),
        "devices": devices,
    }


async def update_group(
    db: AsyncSession, group_id: uuid.UUID, data: DeviceGroupUpdate, *, publisher: EventBus | None = None
) -> DeviceGroup | None:
    stmt = select(DeviceGroup).where(DeviceGroup.id == group_id)
    result = await db.execute(stmt)
    group = result.scalar_one_or_none()
    if group is None:
        return None
    updates = data.model_dump(exclude_unset=True)
    if "filters" in updates:
        group.filters = _dump_filters(data.filters)
        updates.pop("filters")
    for field, value in updates.items():
        setattr(group, field, value)
    queue_event_for_session(
        db,
        "device_group.updated",
        {"group_id": str(group.id), "action": "updated"},
        publisher=publisher or _default_event_bus,
    )
    await db.commit()
    await db.refresh(group)
    return group


async def delete_group(db: AsyncSession, group_id: uuid.UUID, *, publisher: EventBus | None = None) -> bool:
    stmt = select(DeviceGroup).where(DeviceGroup.id == group_id)
    result = await db.execute(stmt)
    group = result.scalar_one_or_none()
    if group is None:
        return False
    await db.delete(group)
    queue_event_for_session(
        db,
        "device_group.updated",
        {"group_id": str(group_id), "action": "deleted"},
        publisher=publisher or _default_event_bus,
    )
    await db.commit()
    return True


async def add_members(
    db: AsyncSession, group_id: uuid.UUID, device_ids: list[uuid.UUID], *, publisher: EventBus | None = None
) -> int:
    if not device_ids:
        return 0
    # Use INSERT ... ON CONFLICT DO NOTHING so a concurrent operator request
    # adding the same (group_id, device_id) degrades to a benign no-op
    # instead of surfacing as IntegrityError on the unique constraint. The
    # previous SELECT-then-add pattern was a TOCTOU between the unlocked
    # exists check and the subsequent insert.
    stmt = (
        pg_insert(DeviceGroupMembership)
        .values([{"group_id": group_id, "device_id": device_id} for device_id in device_ids])
        .on_conflict_do_nothing(index_elements=[DeviceGroupMembership.group_id, DeviceGroupMembership.device_id])
        .returning(DeviceGroupMembership.device_id)
    )
    result = await db.execute(stmt)
    added = len(result.scalars().all())
    if added:
        queue_event_for_session(
            db,
            "device_group.members_changed",
            {"group_id": str(group_id), "added": added},
            publisher=publisher or _default_event_bus,
        )
    await db.commit()
    return added


async def remove_members(
    db: AsyncSession, group_id: uuid.UUID, device_ids: list[uuid.UUID], *, publisher: EventBus | None = None
) -> int:
    stmt = delete(DeviceGroupMembership).where(
        DeviceGroupMembership.group_id == group_id, DeviceGroupMembership.device_id.in_(device_ids)
    )
    result = await db.execute(stmt)
    removed = int(getattr(result, "rowcount", 0) or 0)
    if removed:
        queue_event_for_session(
            db,
            "device_group.members_changed",
            {"group_id": str(group_id), "removed": removed},
            publisher=publisher or _default_event_bus,
        )
    await db.commit()
    return removed


async def get_group_device_ids(db: AsyncSession, group_id: uuid.UUID) -> list[uuid.UUID]:
    stmt = select(DeviceGroup).where(DeviceGroup.id == group_id)
    result = await db.execute(stmt)
    group = result.scalar_one_or_none()
    if group is None:
        return []

    if group.group_type == GroupType.dynamic:
        devices = await _resolve_dynamic_members(db, group.filters or {})
        return [d.id for d in devices]
    else:
        mem_stmt = select(DeviceGroupMembership.device_id).where(DeviceGroupMembership.group_id == group_id)
        mem_result = await db.execute(mem_stmt)
        return [row[0] for row in mem_result.all()]


def _validate_filters(filters_payload: dict[str, Any] | None) -> DeviceGroupFilters:
    return DeviceGroupFilters.model_validate(filters_payload or {})


async def _resolve_dynamic_members(db: AsyncSession, filters_payload: dict[str, Any]) -> list[Device]:
    filters = _validate_filters(filters_payload)
    query_filters = DeviceQueryFilters(**filters.model_dump(exclude_none=True))
    return await device_service.list_devices_by_filters(db, query_filters)


async def _count_dynamic_members(db: AsyncSession, filters_payload: dict[str, Any]) -> int:
    filters = _validate_filters(filters_payload)
    query_filters = DeviceQueryFilters(**filters.model_dump(exclude_none=True))
    return await device_service.count_devices_by_filters(db, query_filters)


def _dump_filters(filters: DeviceGroupFilters | None) -> dict[str, Any] | None:
    if filters is None:
        return None
    return filters.model_dump(mode="json", exclude_none=True)


def _serialize_filters(filters_payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if filters_payload is None:
        return None
    return _validate_filters(filters_payload).model_dump(exclude_none=True)


def _serialize_group(group: DeviceGroup, *, device_count: int) -> dict[str, Any]:
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "group_type": group.group_type.value,
        "filters": _serialize_filters(group.filters),
        "device_count": device_count,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
    }
