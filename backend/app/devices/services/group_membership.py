"""Batch evaluation of device group memberships.

The pure :func:`evaluate_group_memberships` accepts pre-gathered facts and
produces a :class:`GroupMembershipIndex` without issuing any database calls.
The fixed-count :func:`load_group_membership_index` is the single batching
entry point that gathers those facts with a bounded number of reads.

Membership semantics:

- A static group's members are the devices whose static-group-key set contains
  the group's key (sourced from ``DeviceGroupMembership`` rows).
- A dynamic group's members are the devices that satisfy the group's native
  :class:`DeviceGroupFilters` AND belong to every static group its
  ``device_group_member_of`` rows reference. References are supplied to the
  evaluator as a map of source group id -> target group keys; a device missing
  any of them is not a member (the AND short-circuits to empty).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, union
from sqlalchemy.orm import aliased, selectinload

from app.core.timeutil import now_utc
from app.devices.models import (
    Device,
    DeviceGroup,
    DeviceGroupMemberOf,
    DeviceGroupMembership,
    DeviceOperationalState,
    GroupType,
)
from app.devices.schemas.filters import DeviceGroupFilters
from app.devices.services import attention as device_attention
from app.devices.services import readiness as device_readiness
from app.devices.services.state import derive_operational_states
from app.runs.service_reservation import get_device_reservation_map, reservation_gating_run_id

# The dynamic-filter matcher is one return per axis by design; the axis set is
# the public filter contract and collapsing them would obscure the AND semantics.
# ruff: noqa: PLR0911, PLR0912

if TYPE_CHECKING:
    import uuid
    from collections.abc import Collection, Mapping, Sequence

    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.services.readiness import DeviceReadiness
    from app.packs.models import DriverPack


@dataclass(frozen=True, slots=True)
class GroupDefinitionBatch:
    """Everything one group-aware read path needs about a set of group keys."""

    groups: tuple[DeviceGroup, ...]
    member_of_keys_by_dynamic_group_id: Mapping[uuid.UUID, frozenset[str]]


async def load_group_definition_batch(db: AsyncSession, group_keys: Collection[str]) -> GroupDefinitionBatch:
    """One statement: the requested groups, the static groups their relation rows
    reference, and the per-source key map the pure evaluator consumes.

    The reference edges live in ``device_group_member_of``, whose two composite
    foreign keys already pin a source to a dynamic group and a target to a
    static one, so resolving them is a plain join — no recursion, and no
    termination argument to make. Direct keys of any type come back verbatim;
    the referenced static targets are unioned in so the evaluator can resolve a
    dynamic group whose caller never named its targets. Ordered by
    ``DeviceGroup.key`` so callers see a stable sequence.
    """
    keys = sorted({key for key in group_keys if key})
    if not keys:
        return GroupDefinitionBatch(groups=(), member_of_keys_by_dynamic_group_id={})
    direct = select(DeviceGroup.id).where(DeviceGroup.key.in_(keys)).cte("direct_groups")
    wanted = union(
        select(direct.c.id.label("id")),
        select(DeviceGroupMemberOf.static_group_id.label("id")).where(
            DeviceGroupMemberOf.dynamic_group_id.in_(select(direct.c.id))
        ),
    ).cte("wanted_groups")
    target = aliased(DeviceGroup, name="member_of_target")
    stmt = (
        select(DeviceGroup, target.key)
        .join(wanted, wanted.c.id == DeviceGroup.id)
        .outerjoin(DeviceGroupMemberOf, DeviceGroupMemberOf.dynamic_group_id == DeviceGroup.id)
        .outerjoin(target, target.id == DeviceGroupMemberOf.static_group_id)
        .order_by(DeviceGroup.key)
    )
    groups: dict[uuid.UUID, DeviceGroup] = {}
    references: dict[uuid.UUID, set[str]] = {}
    for group, target_key in (await db.execute(stmt)).all():
        groups.setdefault(group.id, group)
        if target_key is not None:
            references.setdefault(group.id, set()).add(str(target_key))
    return GroupDefinitionBatch(
        groups=tuple(groups.values()),
        member_of_keys_by_dynamic_group_id={gid: frozenset(found) for gid, found in references.items()},
    )


async def load_member_of_keys(
    db: AsyncSession, dynamic_group_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, frozenset[str]]:
    """One joined read: relation rows -> target keys, aggregated per source id."""
    ids = sorted(set(dynamic_group_ids))
    if not ids:
        return {}
    stmt = (
        select(DeviceGroupMemberOf.dynamic_group_id, DeviceGroup.key)
        .join(DeviceGroup, DeviceGroup.id == DeviceGroupMemberOf.static_group_id)
        .where(DeviceGroupMemberOf.dynamic_group_id.in_(ids))
    )
    bucket: dict[uuid.UUID, set[str]] = {}
    for source_id, key in (await db.execute(stmt)).all():
        bucket.setdefault(source_id, set()).add(key)
    return {source_id: frozenset(keys) for source_id, keys in bucket.items()}


def static_group_membership_exists(group_key: str) -> ColumnElement[bool]:
    """SQL twin of ``group_key in DeviceGroupFacts.static_group_keys``.

    Correlates on ``Device.id``, so it composes as a WHERE predicate on any
    statement selecting from ``devices``. Restricted to static groups for the
    same reason :func:`load_static_group_keys_by_device_id` is: a dynamic key
    contributes no static membership, so referencing one yields no devices.
    """
    return (
        select(1)
        .select_from(DeviceGroupMembership)
        .join(DeviceGroup, DeviceGroup.id == DeviceGroupMembership.group_id)
        .where(
            DeviceGroupMembership.device_id == Device.id,
            DeviceGroup.key == group_key,
            DeviceGroup.group_type == GroupType.static,
        )
        .exists()
    )


@dataclass(frozen=True)
class DeviceGroupFacts:
    """The per-device inputs the pure evaluator consumes (no IO)."""

    operational_state: DeviceOperationalState
    is_reserved: bool
    readiness_state: str
    needs_attention: bool
    static_group_keys: frozenset[str]


def build_device_group_facts(
    device: Device,
    *,
    operational_state: DeviceOperationalState,
    is_reserved: bool,
    readiness_state: str,
    static_group_keys: frozenset[str],
    review_required: bool | None = None,
) -> DeviceGroupFacts:
    """Derive one device's evaluator facts. Pure: no IO, no session.

    The three fact-gathering call sites (``load_group_membership_index``, the
    grid allocator's ``_facts_from_eligible_rows``, and the run allocator's
    locked step-7b rebuild) legitimately *source* their inputs differently —
    some axes are known by construction from the SQL gate that produced the
    row — but the derivation from those inputs is identical. Keeping it here
    means ``needs_attention`` in particular cannot drift between the paths.

    ``review_required`` defaults to the device row. Callers whose rows provably
    cleared the review gate under a lock pass ``False`` explicitly.
    """
    effective_review_required = bool(device.review_required) if review_required is None else review_required
    needs_attention = device_attention.compute_needs_attention(
        operational_state,
        readiness_state,
        review_required=effective_review_required,
    )
    return DeviceGroupFacts(
        operational_state=operational_state,
        is_reserved=is_reserved,
        readiness_state=readiness_state,
        needs_attention=needs_attention,
        static_group_keys=static_group_keys,
    )


@dataclass(frozen=True)
class GroupMembershipIndex:
    """Read-only map of group key -> set of device ids."""

    by_key: Mapping[str, frozenset[uuid.UUID]]

    def device_ids(self, group_key: str) -> frozenset[uuid.UUID]:
        return self.by_key.get(group_key, frozenset())

    def matches_all(self, device_id: uuid.UUID, group_keys: Collection[str]) -> bool:
        return all(device_id in self.device_ids(key) for key in group_keys)


def _device_matches_dynamic_filters(
    device: Device,
    facts: DeviceGroupFacts,
    filters: DeviceGroupFilters,
    member_of_keys: frozenset[str],
) -> bool:
    """Native filter predicates ANDed with the group's references (static only).

    Mirrors the column-level SQL predicates in
    :mod:`app.devices.services.service` for the axes the group contract exposes,
    so dynamic membership agrees with the device list query at the same instant.
    """
    if filters.pack_id is not None and device.pack_id != filters.pack_id:
        return False
    if filters.platform_id is not None and device.platform_id != filters.platform_id:
        return False
    if filters.status is not None and facts.operational_state.value != filters.status:
        return False
    if filters.reserved is not None and facts.is_reserved != filters.reserved:
        return False
    if filters.host_id is not None and device.host_id != filters.host_id:
        return False
    if filters.identity_value is not None and device.identity_value != filters.identity_value:
        return False
    if filters.connection_target is not None and device.connection_target != filters.connection_target:
        return False
    if filters.device_type is not None and device.device_type != filters.device_type:
        return False
    if filters.connection_type is not None and device.connection_type != filters.connection_type:
        return False
    if filters.os_version is not None and device.os_version != filters.os_version:
        return False
    if filters.os_version_display is not None:
        displayed = device.os_version_display or device.os_version
        if displayed != filters.os_version_display:
            return False
    if filters.needs_attention is not None and facts.needs_attention != filters.needs_attention:
        return False
    # References: AND over static-group keys, supplied by the caller from
    # ``device_group_member_of``. ``filters.member_of`` is never consulted — a
    # row whose JSON still carries the key restricts nothing.
    return member_of_keys <= facts.static_group_keys


def evaluate_group_memberships(
    *,
    groups: Sequence[DeviceGroup],
    devices: Sequence[Device],
    facts_by_device_id: Mapping[uuid.UUID, DeviceGroupFacts],
    member_of_keys_by_dynamic_group_id: Mapping[uuid.UUID, frozenset[str]],
) -> GroupMembershipIndex:
    """Pure batch evaluator. Performs no database IO.

    ``facts_by_device_id`` must contain an entry for every device in ``devices``;
    entries for devices not in the sequence are ignored. The evaluator reads
    only the supplied facts and group definitions.

    ``member_of_keys_by_dynamic_group_id`` is required, not defaulted: this
    function cannot load it, and an empty default would silently widen every
    dynamic group that references a static one at any call site that forgot to
    pass it. Absent ids legitimately mean "this group references nothing".
    """
    memberships: dict[str, frozenset[uuid.UUID]] = {}
    for group in groups:
        if group.group_type == GroupType.static:
            memberships[group.key] = frozenset(
                device.id for device in devices if group.key in facts_by_device_id[device.id].static_group_keys
            )
            continue
        filters = DeviceGroupFilters.model_validate(group.filters or {})
        member_of_keys = member_of_keys_by_dynamic_group_id.get(group.id, frozenset())
        memberships[group.key] = frozenset(
            device.id
            for device in devices
            if _device_matches_dynamic_filters(device, facts_by_device_id[device.id], filters, member_of_keys)
        )
    return GroupMembershipIndex(by_key=memberships)


async def load_static_group_keys_by_device_id(
    db: AsyncSession, device_ids: list[uuid.UUID]
) -> dict[uuid.UUID, frozenset[str]]:
    """One joined read: memberships -> static groups, aggregated per device."""
    if not device_ids:
        return {}
    stmt = (
        select(DeviceGroupMembership.device_id, DeviceGroup.key)
        .join(DeviceGroup, DeviceGroup.id == DeviceGroupMembership.group_id)
        .where(
            DeviceGroupMembership.device_id.in_(device_ids),
            DeviceGroup.group_type == GroupType.static,
        )
    )
    result = await db.execute(stmt)
    bucket: dict[uuid.UUID, set[str]] = {}
    for device_id, key in result.all():
        bucket.setdefault(device_id, set()).add(key)
    return {device_id: frozenset(keys) for device_id, keys in bucket.items()}


async def load_group_membership_index(  # noqa: PLR0913 - one optional injected fact batch per parameter
    db: AsyncSession,
    *,
    groups: Sequence[DeviceGroup],
    devices: Sequence[Device],
    pack_catalog: dict[str, DriverPack] | None = None,
    operational_states: Mapping[uuid.UUID, DeviceOperationalState] | None = None,
    static_group_keys_by_device_id: Mapping[uuid.UUID, frozenset[str]] | None = None,
    readiness_by_device_id: Mapping[uuid.UUID, DeviceReadiness] | None = None,
    reserved_by_device_id: Mapping[uuid.UUID, bool] | None = None,
    member_of_keys_by_dynamic_group_id: Mapping[uuid.UUID, frozenset[str]] | None = None,
) -> GroupMembershipIndex:
    """Fixed-count loader: gather every fact the pure evaluator needs in a
    bounded number of reads, then delegate to :func:`evaluate_group_memberships`.

    Optional injected facts let allocation/run/read paths reuse their own
    already-loaded batches instead of re-reading. When an optional mapping is
    absent the loader reads the category exactly once for the whole batch; when
    every mapping is supplied it issues no query and evaluates purely.

    Reads performed when facts are missing:

    - one pack-catalog load (only when a batch that consumes it — operational
      state or readiness — will actually run and no catalog was supplied),
    - one batch ``derive_operational_states`` (which itself issues one live-
      session lookup, one verification-lease lookup, and a pack-catalog load
      when no catalog is supplied) — only when ``operational_states`` is absent,
    - one batch reservation map, projected through ``reservation_gating_run_id``
      (only when a dynamic group needs native facts and ``reserved_by_device_id``
      is absent),
    - one batch readiness assessment (only when ``readiness_by_device_id`` is absent),
    - one joined static-membership read (only when
      ``static_group_keys_by_device_id`` is absent),
    - one joined ``device_group_member_of`` read over the dynamic ids in
      ``groups`` (only when ``member_of_keys_by_dynamic_group_id`` is absent).
      Callers that already loaded the definitions through
      :func:`load_group_definition_batch` carry its map here and skip it.
    """
    device_list = list(devices)
    device_ids = [d.id for d in device_list]
    if not device_list:
        # Every group is empty with no devices to place in it, so the reference
        # map cannot change the answer — do not buy a read for it.
        return evaluate_group_memberships(
            groups=groups, devices=device_list, facts_by_device_id={}, member_of_keys_by_dynamic_group_id={}
        )

    needs_native_facts = any(g.group_type == GroupType.dynamic for g in groups)
    references: Mapping[uuid.UUID, frozenset[str]]
    if member_of_keys_by_dynamic_group_id is not None:
        references = member_of_keys_by_dynamic_group_id
    elif needs_native_facts:
        references = await load_member_of_keys(db, [g.id for g in groups if g.group_type == GroupType.dynamic])
    else:
        references = {}

    packs = pack_catalog
    # The catalog only feeds derive_operational_states and assess_devices_async.
    # If both operational state and readiness are injected, neither runs, so a
    # catalog load here would be an extra query with no consumer.
    if needs_native_facts and packs is None and (operational_states is None or readiness_by_device_id is None):
        packs = await device_readiness.load_packs_by_ids(db, {d.pack_id for d in device_list if d.pack_id})

    # Ensure appium_node is loaded for every device so device_allows_allocation
    # (called inside derive_operational_states) does not trigger a sync lazy
    # load per device under AsyncSession. Only needed when we actually run
    # derive_operational_states; callers injecting operational_states skip it,
    # as do callers that already loaded the relationship via selectinload.
    if operational_states is None and needs_native_facts:
        unloaded = [d for d in device_list if "appium_node" in sa_inspect(d).unloaded]
        if unloaded:
            reloaded = list(
                (
                    await db.execute(
                        select(Device)
                        .where(Device.id.in_([d.id for d in unloaded]))
                        .options(selectinload(Device.appium_node))
                    )
                )
                .scalars()
                .all()
            )
            by_id = {d.id: d for d in reloaded}
            device_list = [by_id.get(d.id, d) for d in device_list]

    op_map: Mapping[uuid.UUID, DeviceOperationalState]
    if operational_states is None and needs_native_facts:
        op_map = await derive_operational_states(db, device_list, now=now_utc(), packs=packs)
    else:
        op_map = operational_states or {}

    gating_owner_map: Mapping[uuid.UUID, uuid.UUID | None] = {}
    if needs_native_facts and reserved_by_device_id is None:
        # Project the gating owner, not "any active reservation row".
        # ``reservation_gating_run_id`` is the single source for the allocator's
        # gate and the read-side badge — it drops terminal-state runs and
        # effectively-excluded entries — and the grid allocator's SQL twin
        # (``reservation_gating_owner_sql``) feeds the same fact into the same
        # evaluator. Populating ``is_reserved`` any other way would make a
        # dynamic group's ``reserved`` axis disagree with what the allocator
        # actually refuses. Read-side callers inject ``reserved_by_device_id``
        # derived from the same projection, so the axis stays consistent.
        reservation_map = await get_device_reservation_map(db, device_ids)
        gating_owner_map = {
            device_id: reservation_gating_run_id(run, device_id) for device_id, run in reservation_map.items()
        }

    static_keys_map: Mapping[uuid.UUID, frozenset[str]]
    if static_group_keys_by_device_id is None:
        static_keys_map = await load_static_group_keys_by_device_id(db, device_ids)
    else:
        static_keys_map = static_group_keys_by_device_id

    readiness_map: Mapping[uuid.UUID, DeviceReadiness]
    if needs_native_facts and readiness_by_device_id is None:
        readiness_map = await device_readiness.assess_devices_async(db, device_list, packs=packs)
    else:
        readiness_map = readiness_by_device_id or {}

    facts_by_device_id: dict[uuid.UUID, DeviceGroupFacts] = {}
    for device in device_list:
        readiness = readiness_map.get(device.id)
        if reserved_by_device_id is not None:
            is_reserved = reserved_by_device_id.get(device.id, False)
        else:
            is_reserved = gating_owner_map.get(device.id) is not None
        facts_by_device_id[device.id] = build_device_group_facts(
            device,
            operational_state=op_map.get(device.id, DeviceOperationalState.offline),
            is_reserved=is_reserved,
            readiness_state=readiness.readiness_state if readiness is not None else "setup_required",
            static_group_keys=static_keys_map.get(device.id, frozenset()),
        )

    return evaluate_group_memberships(
        groups=groups,
        devices=device_list,
        facts_by_device_id=facts_by_device_id,
        member_of_keys_by_dynamic_group_id=references,
    )
