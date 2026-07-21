from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.core.locks import group_mutation_lock
from app.devices.models import Device, DeviceGroup, DeviceGroupMembership, GroupType
from app.devices.schemas.filters import DeviceGroupFilters
from app.devices.services.group_membership import load_group_membership_index
from app.devices.services.service import device_scope_conditions

if TYPE_CHECKING:
    import uuid
    from collections.abc import Collection, Mapping

    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.protocols import DeviceCrudProtocol
    from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate
    from app.events.protocols import EventPublisher

logger = logging.getLogger(__name__)


class GroupKeyConflictError(ValueError):
    pass


class GroupReferencedError(ValueError):
    """Raised when deleting a group that another group's ``member_of`` references."""

    def __init__(self, dependents: list[str]) -> None:
        self.dependents = dependents
        super().__init__(f"static group is referenced by dynamic groups: {', '.join(dependents)}")


class StaticGroupFiltersError(ValueError):
    """Raised when a static group's payload carries filters.

    Static groups classify by explicit membership only; filters belong to
    dynamic groups. Sibling of :class:`UnknownMemberOfError` — a schema-valid
    body that the domain rejects — and mapped to the same 422.
    """

    def __init__(self) -> None:
        super().__init__("static groups cannot define filters")


class UnknownMemberOfError(ValueError):
    """Raised when a dynamic filter references an unknown or non-static group."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        super().__init__(f"unknown device groups: {', '.join(keys)}")


class DeviceGroupsService:
    """Group definition and membership operations.

    Mutators own the transaction they receive; callers must not stage unrelated
    work on the same session. Definition fields are serialized before commit so
    a peer delete cannot turn a successful write into a misleading 404.
    """

    def __init__(
        self,
        *,
        publisher: EventPublisher,
        crud: DeviceCrudProtocol,
    ) -> None:
        self._publisher = publisher
        self._crud = crud

    async def create_group(self, db: AsyncSession, data: DeviceGroupCreate) -> dict[str, Any]:
        # Only member_of creates resolve peer rows and need definition serialization.
        is_dynamic = data.group_type == GroupType.dynamic
        member_of = _member_of_keys(data.filters) if is_dynamic else set()
        async with group_mutation_lock(db, when=bool(member_of)):
            if member_of:
                _assert_member_of_resolves(data.filters, await _load_groups_by_key(db, member_of))
            elif not is_dynamic and _has_filter_values(data.filters):
                raise StaticGroupFiltersError
            group, payload = await self._insert_group(db, data)
        if is_dynamic:
            payload["device_count"] = await self._dynamic_device_count(db, group)
        return payload

    async def _insert_group(self, db: AsyncSession, data: DeviceGroupCreate) -> tuple[DeviceGroup, dict[str, Any]]:
        """Insert and commit a group, serializing its stable fields before peers can delete it."""
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
            if constraint_name(exc) == "ix_device_groups_key":
                raise GroupKeyConflictError(f"Device group key '{data.key}' already exists") from exc
            raise
        await db.refresh(group)
        self._publisher.queue_for_session(
            db,
            "device_group.updated",
            {"group_key": group.key, "action": "created"},
        )
        payload = _serialize_group(group, device_count=0)
        await db.commit()
        return group, payload

    async def _dynamic_device_count(self, db: AsyncSession, group: DeviceGroup) -> int | None:
        if db.in_transaction():
            raise RuntimeError("dynamic device counts must run outside definition transactions")
        try:
            async with group_mutation_lock(db, when=False):
                devices = await _load_devices_in_scope(db, [group])
                index = await load_group_membership_index(db, groups=[group], devices=devices)
                return len(index.device_ids(group.key))
        except Exception:
            logger.exception("device_group_dynamic_count_failed", extra={"group_key": group.key})
            return None

    async def list_groups(self, db: AsyncSession) -> list[dict[str, Any]]:
        stmt = select(DeviceGroup).order_by(DeviceGroup.name)
        result = await db.execute(stmt)
        groups = list(result.scalars().all())

        # Static counts are an aggregate over membership rows — no device facts
        # involved. Only dynamic groups need the evaluator, and they share one
        # scoped device batch, so neither branch issues a per-group statement.
        static_counts = await _static_member_counts(db) if any(_is_static(g) for g in groups) else {}
        dynamic_groups = [group for group in groups if not _is_static(group)]
        dynamic_counts: dict[str, int] = {}
        if dynamic_groups:
            devices = await _load_devices_in_scope(db, dynamic_groups)
            index = await load_group_membership_index(db, groups=dynamic_groups, devices=devices)
            dynamic_counts = {group.key: len(index.device_ids(group.key)) for group in dynamic_groups}
        return [
            _serialize_group(
                group,
                device_count=static_counts.get(group.key, 0) if _is_static(group) else dynamic_counts[group.key],
            )
            for group in groups
        ]

    async def get_group(self, db: AsyncSession, group_key: str) -> dict[str, Any] | None:
        group = await _get_group_row(db, group_key)
        if group is None:
            return None

        if _is_static(group):
            # Membership rows are the answer for a static group; no device facts
            # are needed, so the evaluator is not involved at all.
            members = await _load_static_members(db, group)
        else:
            devices = await _load_devices_in_scope(db, [group])
            index = await load_group_membership_index(db, groups=[group], devices=devices)
            member_ids = index.device_ids(group.key)
            members = [device for device in devices if device.id in member_ids]
        return {
            **_serialize_group(group, device_count=len(members)),
            "devices": members,
        }

    async def get_group_type(self, db: AsyncSession, group_key: str) -> GroupType | None:
        """The group's type in one row read, or ``None`` when the key is unknown.

        Callers that only need "does this group exist / is it dynamic" must not
        pay :meth:`get_group`'s member load to find out.
        """
        group = await _get_group_row(db, group_key)
        return None if group is None else group.group_type

    async def update_group(self, db: AsyncSession, group_key: str, data: DeviceGroupUpdate) -> dict[str, Any] | None:
        # Updates lock unconditionally because filters can introduce references.
        async with group_mutation_lock(db):
            loaded = await _load_groups_by_key(db, {group_key} | _member_of_keys(data.filters))
            group = loaded.get(group_key)
            if group is None:
                return None
            if group.group_type == GroupType.static:
                # Static groups must not carry filters; reject any filters payload.
                if _has_filter_values(data.filters):
                    raise StaticGroupFiltersError
            elif data.filters is not None:
                _assert_member_of_resolves(data.filters, loaded)
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
            await db.flush()
            await db.refresh(group)
            is_static = _is_static(group)
            if is_static:
                count_stmt = select(func.count(DeviceGroupMembership.device_id)).where(
                    DeviceGroupMembership.group_id == group.id
                )
                device_count = int(await db.scalar(count_stmt) or 0)
            else:
                device_count = 0
            payload = _serialize_group(group, device_count=device_count)
            await db.commit()
        if not is_static:
            payload["device_count"] = await self._dynamic_device_count(db, group)
        return payload

    async def delete_group(self, db: AsyncSession, group_key: str) -> bool:
        async with group_mutation_lock(db):
            # One GIN-backed read loads the target and all member_of candidates.
            stmt = select(DeviceGroup.key, DeviceGroup.filters).where(
                or_(DeviceGroup.key == group_key, DeviceGroup.filters.has_key("member_of"))
            )
            rows = (await db.execute(stmt)).all()
            target_exists = False
            referrers: list[tuple[str, dict[str, Any] | None]] = []
            for key, filters in rows:
                if key == group_key:
                    target_exists = True
                else:
                    referrers.append((key, filters))
            if not target_exists:
                return False
            _assert_no_references(group_key, referrers)
            await db.execute(delete(DeviceGroup).where(DeviceGroup.key == group_key))
            self._publisher.queue_for_session(
                db,
                "device_group.updated",
                {"group_key": group_key, "action": "deleted"},
            )
            await db.commit()
            return True

    async def add_members(self, db: AsyncSession, group_key: str, device_ids: list[uuid.UUID]) -> int | None:
        group = await _get_group_row(db, group_key, for_update=True)
        if group is None:
            # No row matched, so ``FOR UPDATE`` locked nothing — but the read
            # opened a transaction that would otherwise sit until request
            # teardown. End it here.
            await db.rollback()
            return None
        if not device_ids:
            # A row *is* locked on this path. Drop it rather than carrying it
            # through teardown, where it blocks delete_group's DELETE flush.
            await db.rollback()
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
            # See add_members: no row matched, so nothing is locked, but the open
            # transaction still has to end here rather than at teardown.
            await db.rollback()
            return None
        if not device_ids:
            # Same reasoning as add_members' empty-list path: a row *is* locked
            # here, and `device_id IN ()` provably matches nothing, so holding
            # that lock through a no-op DELETE only delays delete_group.
            await db.rollback()
            return 0
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

        if _is_static(group):
            mem_stmt = select(DeviceGroupMembership.device_id).where(DeviceGroupMembership.group_id == group.id)
            mem_result = await db.execute(mem_stmt)
            return [row[0] for row in mem_result.all()]
        devices = await _load_devices_in_scope(db, [group])
        index = await load_group_membership_index(db, groups=[group], devices=devices)
        return list(index.device_ids(group.key))


def _is_static(group: DeviceGroup) -> bool:
    return group.group_type == GroupType.static


async def _static_member_counts(db: AsyncSession) -> dict[str, int]:
    """One aggregate for every static group's member count.

    Deliberately unkeyed by group: a per-group count would be an N+1 across the
    group list, which is what the fleet-wide fact load replaced. Groups with no
    members are absent from the result and read as zero at the call site.
    """
    stmt = (
        select(DeviceGroup.key, func.count(DeviceGroupMembership.device_id))
        .join(DeviceGroupMembership, DeviceGroupMembership.group_id == DeviceGroup.id)
        .where(DeviceGroup.group_type == GroupType.static)
        .group_by(DeviceGroup.key)
    )
    return {key: int(count) for key, count in (await db.execute(stmt)).all()}


async def _load_static_members(db: AsyncSession, group: DeviceGroup) -> list[Device]:
    stmt = (
        select(Device)
        .join(DeviceGroupMembership, DeviceGroupMembership.device_id == Device.id)
        .where(DeviceGroupMembership.group_id == group.id)
        .options(selectinload(Device.appium_node))
        .order_by(Device.created_at, Device.id)
    )
    return list((await db.execute(stmt)).scalars().all())


async def _load_devices_in_scope(db: AsyncSession, dynamic_groups: list[DeviceGroup]) -> list[Device]:
    """One device read bounding the candidates for every supplied dynamic group.

    The per-group scopes are ORed so a single batch serves the whole list.
    Membership itself is still decided live by the evaluator; this only bounds
    what it must consider.

    A group whose filters pin nothing a query can narrow on is *unbounded*: it
    genuinely spans the fleet, so the union with it is the fleet and no arm can
    reduce it. That is inherent, not a bug — but it is worth seeing, because the
    axes that produce it (``status``, ``reserved``, ``needs_attention``) are
    cheap to filter on in the UI and easy to reach by
    accident. Those axes are deliberately excluded from the column scope: their
    SQL twins evaluate at a different instant than the evaluator's facts, so
    narrowing on them could drop a real member. Unbounded groups are therefore
    named in a warning rather than silently widening every co-listed group's
    batch, and the all-narrow case (the common one) stays bounded.
    """
    scopes: list[ColumnElement[bool]] = []
    unbounded: list[str] = []
    for group in dynamic_groups:
        conditions = device_scope_conditions(_validate_filters(group.filters))
        if conditions:
            scopes.append(and_(*conditions))
        else:
            unbounded.append(group.key)
    stmt = select(Device).options(selectinload(Device.appium_node))
    if unbounded:
        logger.warning(
            "device_group_scope_unbounded groups=%s co_listed_narrow_groups=%d "
            "(batch widened to the whole fleet; these groups pin no column-scope axis)",
            sorted(unbounded),
            len(scopes),
        )
    elif scopes:
        stmt = stmt.where(or_(*scopes))
    return list((await db.execute(stmt)).scalars().all())


def _member_of_keys(filters: DeviceGroupFilters | None) -> set[str]:
    """The schema-validated ``member_of`` keys a filters payload references."""
    return set() if filters is None else set(filters.member_of)


async def _load_groups_by_key(db: AsyncSession, keys: Collection[str]) -> dict[str, DeviceGroup]:
    """Load named groups after the advisory lock, without redundant row locks."""
    wanted = {key for key in keys if key}
    if not wanted:
        return {}
    stmt = select(DeviceGroup).where(DeviceGroup.key.in_(wanted)).execution_options(populate_existing=True)
    return {row.key: row for row in (await db.execute(stmt)).scalars().all()}


def _assert_member_of_resolves(filters: DeviceGroupFilters | None, loaded: Mapping[str, DeviceGroup]) -> None:
    """Require every referenced ``member_of`` key to name a known static group."""
    wanted = _member_of_keys(filters)
    if not wanted:
        return
    missing = sorted(wanted - loaded.keys())
    if missing:
        raise UnknownMemberOfError(missing)
    non_static = sorted({key for key in wanted if loaded[key].group_type != GroupType.static})
    if non_static:
        raise UnknownMemberOfError(non_static)


def _assert_no_references(target_key: str, candidates: Collection[tuple[str, dict[str, Any] | None]]) -> None:
    """Reject deletion if any candidate ``(key, filters)`` pair references *target_key*.

    Candidates are every group carrying a ``member_of`` other than the target
    itself; excluding the target is the caller's job. References are read from
    the raw stored value so one malformed row cannot block unrelated deletes.

    Exactly two shapes are read: the well-formed list form
    (``{"member_of": ["target"]}``) and the legacy bare-string form
    (``{"member_of": "target"}``). Other shapes are skipped; only migrations or
    manual SQL can produce them because application writes are schema-validated.

    Membership rows are deliberately ignored because their foreign key carries
    ``ON DELETE CASCADE``.
    """
    dependents: list[str] = []
    for key, filters in candidates:
        if not isinstance(filters, dict):
            # ``filters ? 'member_of'`` is not an object-only predicate: Postgres
            # matches a JSONB *array* containing that string and the bare string
            # itself, so the scan hands us rows whose filters is not a dict at
            # all. Reading ``.get`` off one raises AttributeError inside the
            # advisory lock and 500s every delete in the fleet until that single
            # row is repaired — the blast radius this scan exists to avoid.
            continue
        raw_member_of = filters.get("member_of")
        if isinstance(raw_member_of, list):
            if target_key in raw_member_of:
                dependents.append(key)
        elif raw_member_of == target_key:
            dependents.append(key)
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


def constraint_name(exc: IntegrityError) -> str | None:
    """The DB constraint an IntegrityError violated, unwrapping the driver cause chain.

    Shared with the portability importer, which needs the same key-collision
    discrimination on its own group insert.
    """
    cause: BaseException | None = exc.orig
    while cause is not None:
        name = getattr(cause, "constraint_name", None)
        if isinstance(name, str):
            return name
        cause = cause.__cause__
    return None
