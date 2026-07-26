from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import raiseload, selectinload

from app.core.locks import group_mutation_lock
from app.core.timeutil import now_utc
from app.devices.models import Device, DeviceGroup, DeviceGroupMemberOf, DeviceGroupMembership, GroupType
from app.devices.schemas.filters import DeviceGroupFilters
from app.devices.services.group_membership import (
    load_group_definition_batch,
    load_group_membership_index,
    load_member_of_keys,
)
from app.devices.services.read_projection import load_device_read_projections
from app.devices.services.service import device_scope_conditions

if TYPE_CHECKING:
    import uuid
    from collections.abc import Collection, Mapping
    from datetime import datetime

    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.protocols import DeviceCrudProtocol
    from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate
    from app.devices.services.serialization_types import DeviceReadProjection
    from app.events.protocols import EventPublisher

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeviceGroupDetailLoad:
    """One group-detail read: the serialized group payload, its final member
    devices, and the projection facts that selected them. Carries only frozen
    projections (never ORM rows) into the read path so the synchronous
    serializer can build member DTOs without re-touching the session."""

    payload: dict[str, Any]
    devices: tuple[Device, ...]
    projections: Mapping[uuid.UUID, DeviceReadProjection]


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
        requested_keys = sorted(_member_of_keys(data.filters)) if is_dynamic else []
        async with group_mutation_lock(db, when=bool(requested_keys)):
            targets: dict[str, DeviceGroup] = {}
            if requested_keys:
                targets = await _resolve_static_member_of(db, set(requested_keys))
            elif not is_dynamic and _has_filter_values(data.filters):
                raise StaticGroupFiltersError
            group, payload = await self._insert_group(db, data, targets)
        if is_dynamic:
            payload["device_count"] = await self._dynamic_device_count(db, group)
        return payload

    async def _insert_group(
        self, db: AsyncSession, data: DeviceGroupCreate, targets: Mapping[str, DeviceGroup]
    ) -> tuple[DeviceGroup, dict[str, Any]]:
        """Insert and commit a group, serializing its stable fields before peers can delete it."""
        group = DeviceGroup(
            key=data.key,
            name=data.name,
            description=data.description,
            group_type=GroupType(data.group_type),
            filters=_dump_native_filters(data.filters),
        )
        db.add(group)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            if constraint_name(exc) == "ix_device_groups_key":
                raise GroupKeyConflictError(f"Device group key '{data.key}' already exists") from exc
            raise
        # After the flush, so the source id exists; before the commit, so a
        # rejected reference takes the group row down with it.
        await _replace_member_of(db, group.id, targets, clear_existing=False)
        await db.refresh(group)
        self._publisher.queue_for_session(
            db,
            "device_group.updated",
            {"group_key": group.key, "action": "created"},
        )
        payload = _serialize_group(group, device_count=0, member_of_keys=targets.keys())
        await db.commit()
        return group, payload

    async def _dynamic_device_count(self, db: AsyncSession, group: DeviceGroup) -> int | None:
        if db.in_transaction():
            raise RuntimeError("dynamic device counts must run outside definition transactions")
        # Read the key before the block. Any failure in here leaves an open
        # transaction that the lock's exit rolls back, and a rollback expires
        # every loaded row — so reading ``group.key`` in the handler would need
        # IO from a synchronous context and replace the logged failure with a
        # MissingGreenlet that escapes this method entirely.
        group_key = group.key
        try:
            async with group_mutation_lock(db, when=False):
                references = await load_member_of_keys(db, [group.id])
                devices = await _load_devices_in_scope(db, [group], references)
                index = await load_group_membership_index(
                    db,
                    groups=[group],
                    devices=devices,
                    member_of_keys_by_dynamic_group_id=references,
                )
                return len(index.device_ids(group_key))
        except Exception:
            logger.exception("device_group_dynamic_count_failed", extra={"group_key": group_key})
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
        # This path reads every group, so the keyed batch loader has nothing to
        # narrow on; one id-keyed read serves both the scope query and the
        # evaluator, and doubles as the serializer's reference map.
        references: Mapping[uuid.UUID, frozenset[str]] = {}
        if dynamic_groups:
            references = await load_member_of_keys(db, [group.id for group in dynamic_groups])
            devices = await _load_devices_in_scope(db, dynamic_groups, references)
            index = await load_group_membership_index(
                db,
                groups=dynamic_groups,
                devices=devices,
                member_of_keys_by_dynamic_group_id=references,
            )
            dynamic_counts = {group.key: len(index.device_ids(group.key)) for group in dynamic_groups}
        return [
            _serialize_group(
                group,
                device_count=static_counts.get(group.key, 0) if _is_static(group) else dynamic_counts[group.key],
                member_of_keys=references.get(group.id, frozenset()),
            )
            for group in groups
        ]

    async def load_group_detail(
        self, db: AsyncSession, group_key: str, *, now: datetime
    ) -> DeviceGroupDetailLoad | None:
        """Load a group and its members once, carrying the projection that
        selected them so the read path builds member DTOs synchronously.

        One ``load_device_read_projections`` batch serves both membership
        selection (for a dynamic group) and DTO construction; for a dynamic group
        the membership index reuses those projection facts *and* the reference
        map the definition read already produced, so it issues no extra
        pack/reservation/static/operational-state/``member_of`` query.
        """
        definitions = await load_group_definition_batch(db, [group_key])
        group = next((row for row in definitions.groups if row.key == group_key), None)
        if group is None:
            return None
        references = definitions.member_of_keys_by_dynamic_group_id

        if _is_static(group):
            members = await _load_static_members(db, group)
            projections = await load_device_read_projections(db, members, now=now)
        else:
            candidates = await _load_devices_in_scope(db, [group], references)
            projections = await load_device_read_projections(db, candidates, now=now)
            index = await load_group_membership_index(
                db,
                groups=[group],
                devices=candidates,
                operational_states={d.id: projections[d.id].operational_state for d in candidates},
                static_group_keys_by_device_id={d.id: projections[d.id].static_group_keys for d in candidates},
                readiness_by_device_id={d.id: projections[d.id].readiness for d in candidates},
                reserved_by_device_id={d.id: _reservation_blocks_allocation(projections[d.id]) for d in candidates},
                member_of_keys_by_dynamic_group_id=references,
            )
            member_ids = index.device_ids(group.key)
            members = [device for device in candidates if device.id in member_ids]

        return DeviceGroupDetailLoad(
            payload=_serialize_group(
                group,
                device_count=len(members),
                member_of_keys=references.get(group.id, frozenset()),
            ),
            devices=tuple(members),
            projections={member.id: projections[member.id] for member in members},
        )

    async def get_group(self, db: AsyncSession, group_key: str) -> dict[str, Any] | None:
        """Compatibility wrapper: the serialized payload plus member ORM rows.

        Reuses :meth:`load_group_detail` so no second projection is built.
        """
        detail = await self.load_group_detail(db, group_key, now=now_utc())
        if detail is None:
            return None
        return {**detail.payload, "devices": list(detail.devices)}

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
            group = await _load_group_for_mutation(db, group_key)
            if group is None:
                return None
            updates = data.model_dump(exclude_unset=True)
            replaces_filters = "filters" in updates
            targets: Mapping[str, DeviceGroup] = {}
            member_of_keys: Collection[str] = ()
            if group.group_type == GroupType.static:
                # Static groups must not carry filters; reject any filters payload.
                if _has_filter_values(data.filters):
                    raise StaticGroupFiltersError
            elif replaces_filters:
                targets = await _resolve_static_member_of(db, _member_of_keys(data.filters))
                member_of_keys = targets.keys()
            else:
                # Untouched filters keep the references they already carry, so the
                # payload has to name them even though nothing is being written.
                member_of_keys = (await load_member_of_keys(db, [group.id])).get(group.id, frozenset())
            if replaces_filters:
                group.filters = _dump_native_filters(data.filters)
                updates.pop("filters")
            for field, value in updates.items():
                setattr(group, field, value)
            self._publisher.queue_for_session(
                db,
                "device_group.updated",
                {"group_key": group.key, "action": "updated"},
            )
            await db.flush()
            if replaces_filters and group.group_type == GroupType.dynamic:
                await _replace_member_of(db, group.id, targets)
            await db.refresh(group)
            is_static = _is_static(group)
            if is_static:
                count_stmt = select(func.count(DeviceGroupMembership.device_id)).where(
                    DeviceGroupMembership.group_id == group.id
                )
                device_count = int(await db.scalar(count_stmt) or 0)
            else:
                device_count = 0
            payload = _serialize_group(group, device_count=device_count, member_of_keys=member_of_keys)
            await db.commit()
        if not is_static:
            payload["device_count"] = await self._dynamic_device_count(db, group)
        return payload

    async def delete_group(self, db: AsyncSession, group_key: str) -> bool:
        async with group_mutation_lock(db):
            group = await _get_group_row(db, group_key)
            if group is None:
                return False
            # Bind the id before anything can roll back. A savepoint rollback
            # expires the rows it restores, and reading an expired attribute
            # inside the recovery path needs IO from a synchronous context —
            # which raises MissingGreenlet instead of the error being handled.
            group_id = group.id
            dependents = await _delete_group_or_dependents(db, group_id, group_key)
            if dependents:
                raise GroupReferencedError(dependents)
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
        definitions = await load_group_definition_batch(db, [group_key])
        group = next((row for row in definitions.groups if row.key == group_key), None)
        if group is None:
            return []

        if _is_static(group):
            mem_stmt = select(DeviceGroupMembership.device_id).where(DeviceGroupMembership.group_id == group.id)
            mem_result = await db.execute(mem_stmt)
            return [row[0] for row in mem_result.all()]
        references = definitions.member_of_keys_by_dynamic_group_id
        devices = await _load_devices_in_scope(db, [group], references)
        index = await load_group_membership_index(
            db,
            groups=[group],
            devices=devices,
            member_of_keys_by_dynamic_group_id=references,
        )
        return list(index.device_ids(group.key))


def _is_static(group: DeviceGroup) -> bool:
    return group.group_type == GroupType.static


def _reservation_blocks_allocation(projection: DeviceReadProjection) -> bool:
    """The ``reserved`` axis the dynamic-group evaluator consumes: a live,
    allocation-blocking reservation. Bound to a local so the ``None`` case narrows."""
    reservation = projection.reservation
    return reservation is not None and reservation.blocks_allocation


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
        .options(selectinload(Device.appium_node), raiseload("*"))
        .order_by(Device.created_at, Device.id)
    )
    return list((await db.execute(stmt)).scalars().all())


async def _load_devices_in_scope(
    db: AsyncSession,
    dynamic_groups: list[DeviceGroup],
    member_of_keys_by_dynamic_group_id: Mapping[uuid.UUID, frozenset[str]],
) -> list[Device]:
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
        conditions = device_scope_conditions(
            _validate_filters(group.filters),
            member_of_keys=member_of_keys_by_dynamic_group_id.get(group.id, frozenset()),
        )
        if conditions:
            scopes.append(and_(*conditions))
        else:
            unbounded.append(group.key)
    stmt = select(Device).options(selectinload(Device.appium_node), raiseload("*"))
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
    """The schema-validated ``member_of`` keys a filters payload references.

    A set, because ``DeviceGroupFilters.member_of`` is a plain ``list[GroupKey]``
    with no uniqueness rule: the relation's composite primary key is what makes
    a repeated reference unrepresentable, so the deduplication happens here.
    """
    return set() if filters is None else set(filters.member_of)


async def _load_group_for_mutation(db: AsyncSession, group_key: str) -> DeviceGroup | None:
    """Load a group after the advisory lock, refreshing any preloaded identity.

    ``populate_existing`` matters: a peer that changed ``group_type`` before this
    writer took the lock must be visible, or the static/dynamic branch decides
    against a stale row.
    """
    stmt = select(DeviceGroup).where(DeviceGroup.key == group_key).execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalars().one_or_none()


async def _resolve_static_member_of(db: AsyncSession, keys: set[str]) -> dict[str, DeviceGroup]:
    """Resolve every requested reference key to a static group, or reject the write.

    Runs before the source row is touched so a bad key leaves nothing behind.

    ``populate_existing`` for the same reason :func:`_load_group_for_mutation`
    uses it: a target already in the identity map would otherwise be gated on a
    ``group_type`` a peer has already changed.
    """
    if not keys:
        return {}
    stmt = select(DeviceGroup).where(DeviceGroup.key.in_(keys)).execution_options(populate_existing=True)
    rows = list((await db.execute(stmt)).scalars())
    by_key = {row.key: row for row in rows}
    invalid = sorted(key for key in keys if key not in by_key or by_key[key].group_type != GroupType.static)
    if invalid:
        raise UnknownMemberOfError(invalid)
    return by_key


async def _replace_member_of(
    db: AsyncSession,
    dynamic_group_id: uuid.UUID,
    targets: Mapping[str, DeviceGroup],
    *,
    clear_existing: bool = True,
) -> None:
    """Swap one dynamic group's reference rows inside the caller's transaction.

    Delete-then-insert rather than a diff: the edge set is small, the source is
    already serialised by the group-mutation lock, and a full replacement cannot
    leave a stale edge behind. ``on_conflict_do_nothing`` is deliberately absent
    — the delete guarantees an empty target and the values are deduplicated, so
    a conflict here would be a bug worth surfacing.

    ``clear_existing=False`` is for a freshly inserted source: its id was minted
    in this transaction, so nothing can reference it yet and the delete would be
    a statement that provably matches no row.
    """
    if clear_existing:
        await db.execute(delete(DeviceGroupMemberOf).where(DeviceGroupMemberOf.dynamic_group_id == dynamic_group_id))
    target_ids = sorted({group.id for group in targets.values()})
    if not target_ids:
        return
    stmt = pg_insert(DeviceGroupMemberOf).values(
        [
            {
                "dynamic_group_id": dynamic_group_id,
                "dynamic_group_type": GroupType.dynamic,
                "static_group_id": target_id,
                "static_group_type": GroupType.static,
            }
            for target_id in target_ids
        ]
    )
    try:
        await db.execute(stmt)
        # Flush here so a target deleted between the resolve and now surfaces as
        # a named FK violation at a point this caller still controls, rather than
        # inside an unrelated commit.
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if constraint_name(exc) == "fk_device_group_member_of_static_group":
            raise UnknownMemberOfError(sorted(targets)) from exc
        raise


async def _dependent_dynamic_keys(db: AsyncSession, static_group_id: uuid.UUID) -> list[str]:
    """The keys of the dynamic groups whose relation rows reference *static_group_id*.

    Membership rows are deliberately ignored because their foreign key carries
    ``ON DELETE CASCADE``; only ``device_group_member_of`` is restrictive.
    """
    stmt = (
        select(DeviceGroup.key)
        .join(DeviceGroupMemberOf, DeviceGroupMemberOf.dynamic_group_id == DeviceGroup.id)
        .where(DeviceGroupMemberOf.static_group_id == static_group_id)
    )
    return sorted((await db.execute(stmt)).scalars().all())


async def _try_delete_group_row(db: AsyncSession, group_id: uuid.UUID) -> bool:
    """Delete the definition row; ``False`` when the RESTRICT foreign key stopped it.

    Two things this shape is load-bearing for.

    The ``DELETE`` is inside the ``try``, not a following ``flush``. An
    ``ON DELETE RESTRICT`` foreign key is a non-deferrable AFTER ROW trigger, so
    it fires at the end of the statement that armed it: the violation comes out
    of ``execute``, and a ``try`` wrapped around a later ``flush`` never sees it.

    The statement runs in a SAVEPOINT so the violation does not abort the
    caller's transaction. A root rollback would drop the transaction-scoped
    advisory lock (leaving any follow-up read unserialised) and expire every
    loaded row; rolling back to a savepoint keeps both, so the caller can still
    ask *why* it failed on the same session.
    """
    try:
        async with db.begin_nested():
            await db.execute(delete(DeviceGroup).where(DeviceGroup.id == group_id))
    except IntegrityError as exc:
        if constraint_name(exc) == "fk_device_group_member_of_static_group":
            return False
        raise
    return True


async def _delete_group_or_dependents(db: AsyncSession, group_id: uuid.UUID, group_key: str) -> list[str]:
    """Delete the group's definition row, or name the dependents that stopped it.

    Returns ``[]`` only when the row is gone; a non-empty result is always a
    populated ``GroupReferencedError`` payload, never an empty one.

    The dependent read and the ``DELETE`` are two statements, so a reference
    committed between them is invisible to the first and fatal to the second.
    The foreign key catches that, and the re-read names the referrer. When the
    re-read comes back empty the referrer was inserted *and* removed inside the
    gap, so there is nothing to report and nothing left to block the delete —
    replaying it is the only answer that is neither a 500 nor a 409 naming
    nobody.

    The replay holds ``FOR UPDATE`` on the target row. That conflicts with the
    ``FOR KEY SHARE`` a referencing insert must take on the row it references,
    so no peer can re-arm the foreign key underneath it: the replay is final,
    and the recovery cannot loop.
    """
    dependents = await _dependent_dynamic_keys(db, group_id)
    if dependents:
        return dependents
    if await _try_delete_group_row(db, group_id):
        return []

    dependents = await _dependent_dynamic_keys(db, group_id)
    if dependents:
        return dependents

    if await _get_group_row(db, group_key, for_update=True) is None:
        # A peer deleted the group itself while we were recovering. Reporting it
        # as deleted would be a lie about who did it, but the row is gone either
        # way and the caller's contract only distinguishes "gone" from
        # "referenced".
        return []
    dependents = await _dependent_dynamic_keys(db, group_id)
    if dependents:
        return dependents
    await db.execute(delete(DeviceGroup).where(DeviceGroup.id == group_id))
    return []


def _validate_filters(filters_payload: dict[str, Any] | None) -> DeviceGroupFilters:
    return DeviceGroupFilters.model_validate(filters_payload or {})


def _has_filter_values(filters: DeviceGroupFilters | None) -> bool:
    """True if the filters object pins any axis beyond an empty member_of list."""
    if filters is None:
        return False
    dumped = filters.model_dump(exclude_none=True)
    dumped.pop("member_of", None)
    return bool(dumped) or bool(filters.member_of)


def _dump_native_filters(filters: DeviceGroupFilters | None) -> dict[str, Any] | None:
    """The JSON column's value: the native axes only, never ``member_of``.

    References are rows in ``device_group_member_of`` from this phase on. Storing
    them here too would give the same fact two homes that can disagree.
    """
    if filters is None:
        return None
    dumped = filters.model_dump(mode="json", exclude_none=True)
    dumped.pop("member_of", None)
    return dumped or None


def _serialize_filters(
    filters_payload: dict[str, Any] | None, member_of_keys: Collection[str]
) -> dict[str, Any] | None:
    """Rebuild the public ``filters`` object from the native JSON plus the relation.

    Any stored ``member_of`` is dropped before the merge. The migration leaves
    legacy static rows' JSON untouched precisely because it is inert, so echoing
    it back would advertise a restriction nothing enforces.
    """
    native = dict(filters_payload or {})
    native.pop("member_of", None)
    serialized = _validate_filters(native).model_dump(exclude_none=True)
    serialized.pop("member_of", None)
    keys = sorted(member_of_keys)
    if keys:
        serialized["member_of"] = keys
    return serialized or None


def _serialize_group(group: DeviceGroup, *, device_count: int, member_of_keys: Collection[str]) -> dict[str, Any]:
    return {
        "key": group.key,
        "name": group.name,
        "description": group.description,
        "group_type": group.group_type.value,
        "filters": _serialize_filters(group.filters, member_of_keys),
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
