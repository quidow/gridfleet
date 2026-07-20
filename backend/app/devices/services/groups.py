from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.devices.group_keys import is_valid_group_key
from app.devices.models import Device, DeviceGroup, DeviceGroupMembership, GroupType
from app.devices.schemas.filters import DeviceGroupFilters
from app.devices.services.group_membership import load_group_membership_index

if TYPE_CHECKING:
    import uuid
    from collections.abc import Collection, Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.devices.protocols import DeviceCrudProtocol
    from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate
    from app.events.protocols import EventPublisher


class GroupKeyConflictError(ValueError):
    pass


class GroupReferencedError(ValueError):
    """Raised when deleting a static group that dynamic groups still reference."""

    def __init__(self, dependents: list[str]) -> None:
        self.dependents = dependents
        super().__init__(f"static group is referenced by dynamic groups: {', '.join(dependents)}")


class UnknownMemberOfError(ValueError):
    """Raised when a dynamic filter references an unknown or non-static group."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        super().__init__(f"unknown device groups: {', '.join(keys)}")


class DeviceGroupsService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        crud: DeviceCrudProtocol,
        settings: SettingsReader,
    ) -> None:
        self._publisher = publisher
        self._crud = crud
        self._settings = settings

    async def create_group(self, db: AsyncSession, data: DeviceGroupCreate) -> DeviceGroup:
        if data.group_type == GroupType.dynamic:
            locked = await _lock_groups_by_key(db, _member_of_keys(data.filters))
            _assert_member_of_resolves(data.filters, locked)
        elif _has_filter_values(data.filters):
            raise ValueError("static groups cannot define filters")
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

        # One device batch feeding the pure evaluator. The same facts load is
        # reused for every group definition; counts come from the index.
        device_stmt = select(Device).options(selectinload(Device.appium_node))
        devices = list((await db.execute(device_stmt)).scalars().all())
        index = await load_group_membership_index(db, groups=groups, devices=devices, settings=self._settings)
        return [_serialize_group(group, device_count=len(index.device_ids(group.key))) for group in groups]

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

        device_ids = [m.device_id for m in group.memberships]
        if group.group_type == GroupType.dynamic:
            # Load all devices once and let the evaluator project membership.
            device_stmt = select(Device).options(selectinload(Device.appium_node))
            devices = list((await db.execute(device_stmt)).scalars().all())
        elif not device_ids:
            devices = []
        else:
            device_stmt = select(Device).where(Device.id.in_(device_ids)).options(selectinload(Device.appium_node))
            devices = list((await db.execute(device_stmt)).scalars().all())
        index = await load_group_membership_index(db, groups=[group], devices=devices, settings=self._settings)
        members = [device for device in devices if device.id in index.device_ids(group.key)]
        return {
            **_serialize_group(group, device_count=len(members)),
            "devices": members,
        }

    async def update_group(self, db: AsyncSession, group_key: str, data: DeviceGroupUpdate) -> DeviceGroup | None:
        # One key-ordered lock over the target plus every group its filters
        # reference. Locking the target first and the references second would
        # invert ``delete_group``'s order and deadlock against it.
        locked = await _lock_groups_by_key(db, {group_key} | _member_of_keys(data.filters))
        group = locked.get(group_key)
        if group is None:
            return None
        if group.group_type == GroupType.static:
            # Static groups must not carry filters; reject any filters payload.
            if _has_filter_values(data.filters):
                raise ValueError("static groups cannot define filters")
        elif data.filters is not None:
            _assert_member_of_resolves(data.filters, locked)
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
        # One key-ordered lock over the target and every possible referrer (any
        # group carrying a ``member_of``), so this never pre-locks the target
        # and then queues behind a row a concurrent ``update_group`` already
        # holds. Both operations acquire ``device_groups`` rows in one
        # ascending-key statement, which makes a lock cycle impossible.
        stmt = (
            select(DeviceGroup)
            .where(or_(DeviceGroup.key == group_key, DeviceGroup.filters["member_of"].is_not(None)))
            .order_by(DeviceGroup.key)
            .with_for_update()
        )
        rows = list((await db.execute(stmt)).scalars().all())
        group = next((row for row in rows if row.key == group_key), None)
        if group is None:
            return False
        if group.group_type == GroupType.static:
            _assert_no_references(group_key, rows)
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
            devices = list((await db.execute(select(Device))).scalars().all())
            index = await load_group_membership_index(db, groups=[group], devices=devices, settings=self._settings)
            return list(index.device_ids(group.key))
        else:
            mem_stmt = select(DeviceGroupMembership.device_id).where(DeviceGroupMembership.group_id == group.id)
            mem_result = await db.execute(mem_stmt)
            return [row[0] for row in mem_result.all()]


def _member_of_keys(filters: DeviceGroupFilters | None) -> set[str]:
    """The well-formed ``member_of`` keys a filters payload references.

    Malformed keys surface as 422 from the schema, so they are dropped here
    rather than sent to the lock statement.
    """
    if filters is None:
        return set()
    return {key for key in filters.member_of if is_valid_group_key(key)}


async def _lock_groups_by_key(db: AsyncSession, keys: Collection[str]) -> dict[str, DeviceGroup]:
    """Lock the named ``device_groups`` rows in one ascending-key statement.

    Every multi-row ``device_groups`` lock in this service goes through a single
    key-ordered ``FOR UPDATE`` (``LockRows`` sits above ``Sort``, so rows are
    locked in key order). One global lock order acquired in one statement is
    what keeps concurrent group edits from forming a cycle — locking a target
    row first and its references second is exactly the inversion that deadlocked
    ``delete_group`` against ``update_group``.
    """
    wanted = sorted({key for key in keys if key})
    if not wanted:
        return {}
    stmt = select(DeviceGroup).where(DeviceGroup.key.in_(wanted)).order_by(DeviceGroup.key).with_for_update()
    return {row.key: row for row in (await db.execute(stmt)).scalars().all()}


def _assert_member_of_resolves(filters: DeviceGroupFilters | None, locked: Mapping[str, DeviceGroup]) -> None:
    """Require every referenced ``member_of`` key to name a locked static group."""
    wanted = _member_of_keys(filters)
    if not wanted:
        return
    missing = sorted(wanted - locked.keys())
    if missing:
        raise UnknownMemberOfError(missing)
    non_static = sorted({key for key in wanted if locked[key].group_type != GroupType.static})
    if non_static:
        raise UnknownMemberOfError(non_static)


def _assert_no_references(target_key: str, locked_rows: Collection[DeviceGroup]) -> None:
    """Reject deletion if any locked group references *target_key*.

    Scans every group carrying a ``member_of``, not just dynamic ones: nothing
    enforces that static groups have no ``member_of``, and the tag migration
    rewrote ``filters`` for any group with a ``tags`` key regardless of type, so
    a static group can carry one. Skipping those would leave a dangling
    reference behind.
    """
    dependents = [
        group.key
        for group in locked_rows
        if group.key != target_key and target_key in DeviceGroupFilters.model_validate(group.filters or {}).member_of
    ]
    if dependents:
        raise GroupReferencedError(sorted(dependents))


def _validate_filters(filters_payload: dict[str, Any] | None) -> DeviceGroupFilters:
    return DeviceGroupFilters.model_validate(filters_payload or {})


def _has_filter_values(filters: DeviceGroupFilters | None) -> bool:
    """True if the filters object pins any axis beyond an empty member_of list."""
    if filters is None:
        return False
    dumped = filters.model_dump(exclude_none=True)
    dumped.pop("member_of", None)
    return bool(dumped) or bool(filters.member_of)


def _dump_filters(filters: DeviceGroupFilters | None) -> dict[str, Any] | None:
    if filters is None:
        return None
    dumped = filters.model_dump(mode="json", exclude_none=True)
    if not dumped.get("member_of"):
        dumped.pop("member_of", None)
    return dumped


def _serialize_filters(filters_payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if filters_payload is None:
        return None
    serialized = _validate_filters(filters_payload).model_dump(exclude_none=True)
    if not serialized.get("member_of"):
        serialized.pop("member_of", None)
    return serialized


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
