from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.devices.models import Device, DeviceGroup, DeviceGroupMembership, GroupType
from app.devices.schemas.filters import DeviceGroupFilters, DeviceQueryFilters

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.protocols import DeviceCrudProtocol
    from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate
    from app.events.protocols import EventPublisher


class GroupKeyConflictError(ValueError):
    pass


class DeviceGroupsService:
    def __init__(self, *, publisher: EventPublisher, crud: DeviceCrudProtocol) -> None:
        self._publisher = publisher
        self._crud = crud

    async def create_group(self, db: AsyncSession, data: DeviceGroupCreate) -> DeviceGroup:
        group = DeviceGroup(
            key=data.key,
            name=data.name,
            description=data.description,
            group_type=GroupType(data.group_type),
            filters=_dump_filters(data.filters),
        )
        db.add(group)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            if _constraint_name(exc) == "ix_device_groups_key":
                raise GroupKeyConflictError(f"Device group key '{data.key}' already exists") from exc
            raise
        self._publisher.queue_for_session(
            db,
            "device_group.updated",
            {"group_key": group.key, "action": "created"},
        )
        await db.commit()
        await db.refresh(group)
        return group

    async def list_groups(self, db: AsyncSession) -> list[dict[str, Any]]:
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
                count = await _count_dynamic_members(db, group.filters or {}, crud=self._crud)
            else:
                count = static_counts.get(group.id, 0)

            output.append(_serialize_group(group, device_count=count))
        return output

    async def get_group(self, db: AsyncSession, group_key: str) -> dict[str, Any] | None:
        stmt = (
            select(DeviceGroup)
            .where(DeviceGroup.key == group_key)
            .options(selectinload(DeviceGroup.memberships).selectinload(DeviceGroupMembership.device))
        )
        result = await db.execute(stmt)
        group = result.scalar_one_or_none()
        if group is None:
            return None

        if group.group_type == GroupType.dynamic:
            devices = await _resolve_dynamic_members(db, group.filters or {}, crud=self._crud)
        else:
            devices = [m.device for m in group.memberships if m.device is not None]

        return {
            **_serialize_group(group, device_count=len(devices)),
            "devices": devices,
        }

    async def update_group(self, db: AsyncSession, group_key: str, data: DeviceGroupUpdate) -> DeviceGroup | None:
        group = await _get_group_row(db, group_key, for_update=True)
        if group is None:
            return None
        updates = data.model_dump(exclude_unset=True)
        if "filters" in updates:
            group.filters = _dump_filters(data.filters)
            updates.pop("filters")
        for field, value in updates.items():
            setattr(group, field, value)
        self._publisher.queue_for_session(
            db,
            "device_group.updated",
            {"group_key": group.key, "action": "updated"},
        )
        await db.commit()
        await db.refresh(group)
        return group

    async def delete_group(self, db: AsyncSession, group_key: str) -> bool:
        group = await _get_group_row(db, group_key, for_update=True)
        if group is None:
            return False
        await db.delete(group)
        self._publisher.queue_for_session(
            db,
            "device_group.updated",
            {"group_key": group.key, "action": "deleted"},
        )
        await db.commit()
        return True

    async def add_members(self, db: AsyncSession, group_key: str, device_ids: list[uuid.UUID]) -> int | None:
        group = await _get_group_row(db, group_key, for_update=True)
        if group is None:
            return None
        if not device_ids:
            return 0
        # Use INSERT ... ON CONFLICT DO NOTHING so a concurrent operator request
        # adding the same (group_id, device_id) degrades to a benign no-op
        # instead of surfacing as IntegrityError on the unique constraint. The
        # previous SELECT-then-add pattern was a TOCTOU between the unlocked
        # exists check and the subsequent insert.
        stmt = (
            pg_insert(DeviceGroupMembership)
            .values([{"group_id": group.id, "device_id": device_id} for device_id in device_ids])
            .on_conflict_do_nothing(index_elements=[DeviceGroupMembership.group_id, DeviceGroupMembership.device_id])
            .returning(DeviceGroupMembership.device_id)
        )
        result = await db.execute(stmt)
        added = len(result.scalars().all())
        if added:
            self._publisher.queue_for_session(
                db,
                "device_group.members_changed",
                {"group_key": group.key, "added": added},
            )
        await db.commit()
        return added

    async def remove_members(self, db: AsyncSession, group_key: str, device_ids: list[uuid.UUID]) -> int | None:
        group = await _get_group_row(db, group_key, for_update=True)
        if group is None:
            return None
        stmt = delete(DeviceGroupMembership).where(
            DeviceGroupMembership.group_id == group.id, DeviceGroupMembership.device_id.in_(device_ids)
        )
        result = await db.execute(stmt)
        removed = int(getattr(result, "rowcount", 0) or 0)
        if removed:
            self._publisher.queue_for_session(
                db,
                "device_group.members_changed",
                {"group_key": group.key, "removed": removed},
            )
        await db.commit()
        return removed

    async def get_group_device_ids(self, db: AsyncSession, group_key: str) -> list[uuid.UUID]:
        group = await _get_group_row(db, group_key)
        if group is None:
            return []

        if group.group_type == GroupType.dynamic:
            devices = await _resolve_dynamic_members(db, group.filters or {}, crud=self._crud)
            return [d.id for d in devices]
        else:
            mem_stmt = select(DeviceGroupMembership.device_id).where(DeviceGroupMembership.group_id == group.id)
            mem_result = await db.execute(mem_stmt)
            return [row[0] for row in mem_result.all()]


def _validate_filters(filters_payload: dict[str, Any] | None) -> DeviceGroupFilters:
    return DeviceGroupFilters.model_validate(filters_payload or {})


async def _resolve_dynamic_members(
    db: AsyncSession, filters_payload: dict[str, Any], *, crud: DeviceCrudProtocol
) -> list[Device]:
    filters = _validate_filters(filters_payload)
    query_filters = DeviceQueryFilters(**filters.model_dump(exclude_none=True))
    return await crud.list_devices_by_filters(db, query_filters)


async def _count_dynamic_members(db: AsyncSession, filters_payload: dict[str, Any], *, crud: DeviceCrudProtocol) -> int:
    filters = _validate_filters(filters_payload)
    query_filters = DeviceQueryFilters(**filters.model_dump(exclude_none=True))
    return await crud.count_devices_by_filters(db, query_filters)


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
        "key": group.key,
        "name": group.name,
        "description": group.description,
        "group_type": group.group_type.value,
        "filters": _serialize_filters(group.filters),
        "device_count": device_count,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
    }


async def _get_group_row(db: AsyncSession, group_key: str, *, for_update: bool = False) -> DeviceGroup | None:
    stmt = select(DeviceGroup).where(DeviceGroup.key == group_key)
    if for_update:
        stmt = stmt.with_for_update()
    return cast("DeviceGroup | None", await db.scalar(stmt))


def _constraint_name(exc: IntegrityError) -> str | None:
    cause: BaseException | None = exc.orig
    while cause is not None:
        name = getattr(cause, "constraint_name", None)
        if isinstance(name, str):
            return name
        cause = cause.__cause__
    return None
