from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import raiseload, selectinload

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


@dataclass(frozen=True, slots=True)
class GroupWriteResult:
    """One group mutation's committed outcome, carrying no ORM row.

    ``payload`` is serialized inside the write transaction so a peer delete
    cannot turn a successful write into a misleading 404. ``group_id`` and
    ``group_key`` are the scalars the caller needs for the post-commit dynamic
    count, which must not run inside the write boundary: it is a fleet-wide
    evaluator read and would hold the definition row lock across it.
    """

    payload: dict[str, Any]
    group_id: uuid.UUID
    group_key: str
    is_dynamic: bool


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

    async def create_group(self, db: AsyncSession, data: DeviceGroupCreate) -> GroupWriteResult:
        """Insert one group and its references inside the caller's transaction.

        The caller owns the boundary (``app/devices/routers/groups.py``). Every
        rejection below raises, and the caller's ``begin()`` block is what undoes
        the partial work — this method never rolls back, so a rejection cannot
        reach past the caller and discard work it staged.
        """
        is_dynamic = data.group_type == GroupType.dynamic
        requested_keys = _member_of_keys(data.filters) if is_dynamic else set()
        targets: dict[str, DeviceGroup] = {}
        if requested_keys:
            targets = await _resolve_static_member_of(db, requested_keys)
        elif not is_dynamic and _has_filter_values(data.filters):
            raise StaticGroupFiltersError
        return await self._insert_group(db, data, targets)

    async def _insert_group(
        self, db: AsyncSession, data: DeviceGroupCreate, targets: Mapping[str, DeviceGroup]
    ) -> GroupWriteResult:
        """Insert a group, serializing its stable fields before the caller commits."""
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
            # No rollback here: the flush aborted the caller's transaction and the
            # caller's boundary is what ends it. ``constraint_name`` reads the
            # exception's own driver chain, so the discrimination below needs no
            # further statement on the aborted session.
            if constraint_name(exc) == "ix_device_groups_key":
                raise GroupKeyConflictError(f"Device group key '{data.key}' already exists") from exc
            raise
        # After the flush, so the source id exists; before the caller's commit, so
        # a rejected reference takes the group row down with it.
        await _replace_member_of(db, group.id, targets, clear_existing=False)
        await db.refresh(group)
        self._publisher.queue_for_session(
            db,
            "device_group.updated",
            {"group_key": group.key, "action": "created"},
        )
        return GroupWriteResult(
            payload=_serialize_group(group, device_count=0, member_of_keys=targets.keys()),
            group_id=group.id,
            group_key=group.key,
            is_dynamic=group.group_type == GroupType.dynamic,
        )

    async def dynamic_device_count(self, db: AsyncSession, *, group_id: uuid.UUID, group_key: str) -> int | None:
        """A dynamic group's live member count, or ``None`` when the read fails.

        Runs on a throwaway read session the caller opens *after* the write
        committed, never inside the write boundary. A failure here aborts only
        that session, and the null count it returns is the documented public
        shape (``DeviceGroupMutationRead.device_count`` is ``int | None``) — a
        count that could not be computed must not fail a write that succeeded.
        """
        try:
            references = await load_member_of_keys(db, [group_id])
            groups = list((await db.execute(select(DeviceGroup).where(DeviceGroup.id == group_id))).scalars())
            if not groups:
                return None
            devices = await _load_devices_in_scope(db, groups, references)
            index = await load_group_membership_index(
                db,
                groups=groups,
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

    async def update_group(self, db: AsyncSession, group_key: str, data: DeviceGroupUpdate) -> GroupWriteResult | None:
        """Apply one group update inside the caller's transaction, or ``None`` for an unknown key."""
        group = await _load_group_for_mutation(db, group_key)
        if group is None:
            return None
        updates = data.model_dump(exclude_unset=True)
        replaces_filters = "filters" in updates
        targets, member_of_keys = await _resolve_update_references(db, group, data, replaces_filters=replaces_filters)
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
        return GroupWriteResult(
            payload=_serialize_group(group, device_count=device_count, member_of_keys=member_of_keys),
            group_id=group.id,
            group_key=group.key,
            is_dynamic=not is_static,
        )

    async def delete_group(self, db: AsyncSession, group_key: str) -> bool:
        # ``FOR UPDATE`` before anything else reads, and it is doing three
        # jobs at once.
        #
        # Ordering: it is the parent half of the parent-before-edge rule
        # every group-definition writer follows (see ``_replace_member_of``).
        # Taking it here, rather than letting the ``DELETE`` take it later,
        # is what keeps this writer from holding a ``device_groups`` row
        # while a reference writer holds a ``device_group_member_of`` tuple
        # and each waits for the other's.
        #
        # Exclusion: inserting a reference to this group requires
        # ``FOR KEY SHARE`` on this row, which conflicts. So no edge can be
        # committed against it from here on, the dependent read below sees
        # every edge that will ever exist for this delete, and the
        # ``ON DELETE RESTRICT`` foreign key cannot fire.
        #
        # Identity: the row is matched by key but deleted by id. Holding it
        # is what stops a peer from deleting and recreating the key
        # underneath us, which would leave the ``DELETE`` matching nothing
        # while this call still reported success. A peer that got there
        # first makes this read return no row at all — EvalPlanQual re-checks
        # the locked tuple and finds it deleted — so a concurrent duplicate
        # delete gets the 404 it should rather than a second success.
        group = await _get_group_row(db, group_key, for_update=True)
        if group is None:
            return False
        group_id = group.id
        dependents = await _dependent_dynamic_keys(db, group_id)
        if dependents:
            # Raised inside the caller's transaction; the caller's ``begin()``
            # block releases this row lock on the way out.
            raise GroupReferencedError(dependents)
        await db.execute(delete(DeviceGroup).where(DeviceGroup.id == group_id))
        self._queue_deleted_event(db, group_key)
        return True

    def _queue_deleted_event(self, db: AsyncSession, group_key: str) -> None:
        """Named seam so a test can inject a failure between the DELETE and the commit."""
        self._publisher.queue_for_session(
            db,
            "device_group.updated",
            {"group_key": group_key, "action": "deleted"},
        )

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
    """Lock and load a group for mutation, refreshing any preloaded identity.

    ``FOR UPDATE`` is what serialises two ``update_group`` calls against the
    same row, and it has to be taken here rather than relied on as a side effect
    of the flush. A ``member_of``-only payload leaves ``filters`` at the value it
    already had (references live in ``device_group_member_of``, so
    ``_dump_native_filters`` returns the same ``None``), and SQLAlchemy omits an
    unchanged column from the UPDATE — with no other field in the payload it
    emits no statement against ``device_groups`` at all. Without this lock both
    writers then run ``_replace_member_of``'s delete-then-insert against a row
    neither has claimed, and the loser's DELETE cannot see edges the winner had
    not committed when it planned: the two reference sets end up unioned rather
    than the last writer winning.

    ``populate_existing`` matters for the same reason the lock does: a peer that
    changed ``group_type`` since this session last loaded the row must be
    visible, or the static/dynamic branch decides against a stale copy in the
    identity map.
    """
    stmt = (
        select(DeviceGroup)
        .where(DeviceGroup.key == group_key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalars().one_or_none()


async def _resolve_update_references(
    db: AsyncSession,
    group: DeviceGroup,
    data: DeviceGroupUpdate,
    *,
    replaces_filters: bool,
) -> tuple[Mapping[str, DeviceGroup], Collection[str]]:
    """The reference rows an update must write, and the keys its payload must echo.

    Split out of :meth:`DeviceGroupsService.update_group` only to keep that
    method's branch count under the lint ceiling; it has no other caller and no
    meaning outside that one step.
    """
    if group.group_type == GroupType.static:
        # Static groups must not carry filters; reject any filters payload.
        if _has_filter_values(data.filters):
            raise StaticGroupFiltersError
        return {}, ()
    if replaces_filters:
        targets = await _resolve_static_member_of(db, _member_of_keys(data.filters))
        return targets, targets.keys()
    # Untouched filters keep the references they already carry, so the payload
    # has to name them even though nothing is being written.
    return {}, (await load_member_of_keys(db, [group.id])).get(group.id, frozenset())


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

    Delete-then-insert rather than a diff: the edge set is small and a full
    replacement cannot leave a stale edge behind. ``on_conflict_do_nothing`` is
    deliberately absent — the delete guarantees an empty target and the values
    are deduplicated, so a conflict here would be a bug worth surfacing.

    The delete-then-insert pair is only last-writer-wins because the caller
    reaches it holding the source row's ``FOR UPDATE`` (see
    :func:`_load_group_for_mutation`, or a source id minted in this transaction
    on the ``clear_existing=False`` path). Two unserialised callers would each
    delete edges the other had not yet committed and then insert their own,
    leaving the union.

    ``clear_existing=False`` is for a freshly inserted source: its id was minted
    in this transaction, so nothing can reference it yet and the delete would be
    a statement that provably matches no row.

    The INSERT runs in a SAVEPOINT because it can still fail: the pre-lock below
    orders the acquisition but deliberately does not verify it, so a target
    deleted between :func:`_resolve_static_member_of` and here is still caught by
    the foreign key rather than by a second application check. A root rollback
    would reach past this function and discard whatever the caller had staged —
    ``update_group`` arrives with a flushed field update and a queued event —
    and would expire every loaded row, from inside a helper the caller cannot
    see into. Rolling back to a savepoint confines the undo to the statement
    that failed and leaves the abort decision where it belongs.
    """
    target_ids = sorted({group.id for group in targets.values()})
    # Parent rows before edges, in a deterministic order.
    #
    # The INSERT below has to take ``FOR KEY SHARE`` on each target anyway — that
    # is how PostgreSQL enforces the foreign key. Taking it here instead means
    # this writer never holds a ``device_group_member_of`` tuple (which the
    # ``DELETE`` that follows locks) while still wanting a ``device_groups`` row,
    # which is the only shape that can deadlock against ``delete_group``. Sorted
    # by id so two callers with overlapping target sets cannot deadlock against
    # each other either.
    if target_ids:
        await db.execute(
            select(DeviceGroup.id)
            .where(DeviceGroup.id.in_(target_ids))
            .order_by(DeviceGroup.id)
            .with_for_update(read=True, key_share=True)
        )
    if clear_existing:
        await db.execute(delete(DeviceGroupMemberOf).where(DeviceGroupMemberOf.dynamic_group_id == dynamic_group_id))
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
        async with db.begin_nested():
            # Both foreign keys are NOT DEFERRABLE, so the RI check runs as an
            # AFTER-ROW trigger that fires before this call returns — a target
            # deleted between the resolve and now surfaces as a named FK
            # violation right here, inside the savepoint this caller controls,
            # rather than inside an unrelated commit.
            await db.execute(stmt)
    except IntegrityError as exc:
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
