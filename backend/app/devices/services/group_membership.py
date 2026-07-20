"""Batch evaluation of device group memberships.

The pure :func:`evaluate_group_memberships` accepts pre-gathered facts and
produces a :class:`GroupMembershipIndex` without issuing any database calls.
The fixed-count :func:`load_group_membership_index` is the single batching
entry point that gathers those facts with a bounded number of reads.

Membership semantics:

- A static group's members are the devices whose static-group-key set contains
  the group's key (sourced from ``DeviceGroupMembership`` rows).
- A dynamic group's members are the devices that satisfy the group's native
  :class:`DeviceGroupFilters` AND belong to every static group listed in the
  filter's ``member_of``. References to dynamic or unknown keys contribute no
  devices (the AND short-circuits to empty).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select, true
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import selectinload

from app.core.timeutil import now_utc
from app.devices.models import Device, DeviceGroup, DeviceGroupMembership, DeviceOperationalState, GroupType
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

    from app.packs.models import DriverPack


async def load_groups_by_keys(db: AsyncSession, group_keys: Collection[str]) -> list[DeviceGroup]:
    """One read: the requested groups plus the static groups their JSON
    ``member_of`` arrays reference, so the pure evaluator can resolve dynamic
    groups that reference static groups by key. Direct keys of any type are
    returned verbatim; only static groups are pulled from ``member_of``
    (dynamic-to-dynamic references resolve to empty membership by contract).

    Implemented as a single recursive CTE. Postgres requires the recursive
    ``group_closure`` reference to appear in the FROM clause of the recursive
    arm (it may not live in a subquery), so the arm joins the closure to the
    set-returning ``jsonb_array_elements_text(filters->'member_of')`` and then
    to ``device_groups`` on the resulting key. Static groups have no
    ``member_of`` (and dynamic groups are not valid ``member_of`` targets),
    so the recursion terminates at static groups; ``jsonb_array_elements_text``
    on a NULL/missing ``member_of`` yields zero rows, also terminating the
    recursion for groups without a ``member_of`` reference.
    """
    keys = sorted({key for key in group_keys if key})
    if not keys:
        return []
    seed = select(DeviceGroup.key, DeviceGroup.filters).where(DeviceGroup.key.in_(keys))
    closure = seed.cte("group_closure", recursive=True)
    closure_alias = closure.alias()
    member_of_keys = (
        func.jsonb_array_elements_text(closure_alias.c.filters["member_of"])
        .table_valued("member_of_key")
        .render_derived()
    )
    arm = (
        select(DeviceGroup.key, DeviceGroup.filters)
        .select_from(closure_alias)
        .join(member_of_keys, true())
        .join(DeviceGroup, DeviceGroup.key == member_of_keys.c.member_of_key)
        .where(DeviceGroup.group_type == GroupType.static)
    )
    # ``union`` (not ``union_all``): termination must be structural, not an
    # assumption about the data. Static groups are not supposed to carry a
    # ``member_of``, but nothing enforces that — the tag migration rewrote
    # ``filters`` for any group with a ``tags`` key regardless of type — so a
    # static group carrying one could cycle a UNION ALL recursion forever.
    # Deduplicating rows makes the recursion terminate on any graph.
    closure = closure.union(arm)
    stmt = select(DeviceGroup).where(DeviceGroup.key.in_(select(closure.c.key)))
    return list((await db.execute(stmt)).scalars().all())


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


def _device_matches_dynamic_filters(device: Device, facts: DeviceGroupFacts, filters: DeviceGroupFilters) -> bool:
    """Native filter predicates ANDed with ``member_of`` (static references only).

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
    # member_of: AND over static-group keys. Dynamic or unknown keys contribute
    # no devices (set membership fails), matching the spec's "references to
    # static groups only" contract.
    return not (filters.member_of and not set(filters.member_of) <= facts.static_group_keys)


def evaluate_group_memberships(
    *,
    groups: Sequence[DeviceGroup],
    devices: Sequence[Device],
    facts_by_device_id: Mapping[uuid.UUID, DeviceGroupFacts],
) -> GroupMembershipIndex:
    """Pure batch evaluator. Performs no database IO.

    ``facts_by_device_id`` must contain an entry for every device in ``devices``;
    entries for devices not in the sequence are ignored. The evaluator reads
    only the supplied facts and group definitions.
    """
    memberships: dict[str, frozenset[uuid.UUID]] = {}
    for group in groups:
        if group.group_type == GroupType.static:
            memberships[group.key] = frozenset(
                device.id for device in devices if group.key in facts_by_device_id[device.id].static_group_keys
            )
            continue
        filters = DeviceGroupFilters.model_validate(group.filters or {})
        memberships[group.key] = frozenset(
            device.id
            for device in devices
            if _device_matches_dynamic_filters(device, facts_by_device_id[device.id], filters)
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


async def load_group_membership_index(
    db: AsyncSession,
    *,
    groups: Sequence[DeviceGroup],
    devices: Sequence[Device],
    pack_catalog: dict[str, DriverPack] | None = None,
    operational_states: Mapping[uuid.UUID, DeviceOperationalState] | None = None,
    static_group_keys_by_device_id: Mapping[uuid.UUID, frozenset[str]] | None = None,
) -> GroupMembershipIndex:
    """Fixed-count loader: gather every fact the pure evaluator needs in a
    bounded number of reads, then delegate to :func:`evaluate_group_memberships`.

    Optional injected facts let allocation/run paths reuse their own already-
    loaded batches instead of re-reading. When an optional mapping is absent
    the loader reads the category exactly once for the whole batch.

    Reads performed when facts are missing:

    - one pack-catalog load (only when readiness is needed and no catalog was
      supplied),
    - one batch ``derive_operational_states`` (which itself issues one live-
      session lookup, one verification-lease lookup, and a pack-catalog load
      when no catalog is supplied),
    - one batch reservation map, projected through ``reservation_gating_run_id``
      (only when a dynamic group needs native facts),
    - one joined static-membership read (only when
      ``static_group_keys_by_device_id`` is absent).
    """
    device_list = list(devices)
    device_ids = [d.id for d in device_list]
    if not device_list:
        return evaluate_group_memberships(groups=groups, devices=device_list, facts_by_device_id={})

    needs_native_facts = any(g.group_type == GroupType.dynamic for g in groups)
    packs = pack_catalog
    if needs_native_facts and packs is None:
        packs = await device_readiness.load_packs_by_ids(db, {d.pack_id for d in device_list if d.pack_id})

    # Ensure appium_node is loaded for every device so device_allows_allocation
    # (called inside derive_operational_states) does not trigger a sync lazy
    # load per device under AsyncSession. Callers that already loaded the
    # relationship (e.g. the group-detail router via selectinload) skip this.
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
    if needs_native_facts:
        # Project the gating owner, not "any active reservation row".
        # ``reservation_gating_run_id`` is the single source for the allocator's
        # gate and the read-side badge — it drops terminal-state runs and
        # effectively-excluded entries — and the grid allocator's SQL twin
        # (``reservation_gating_owner_sql``) feeds the same fact into the same
        # evaluator. Populating ``is_reserved`` any other way would make a
        # dynamic group's ``reserved`` axis disagree with what the allocator
        # actually refuses.
        reservation_map = await get_device_reservation_map(db, device_ids)
        gating_owner_map = {
            device_id: reservation_gating_run_id(run, device_id) for device_id, run in reservation_map.items()
        }

    static_keys_map: Mapping[uuid.UUID, frozenset[str]]
    if static_group_keys_by_device_id is None:
        static_keys_map = await load_static_group_keys_by_device_id(db, device_ids)
    else:
        static_keys_map = static_group_keys_by_device_id

    if needs_native_facts:
        readiness_map = await device_readiness.assess_devices_async(db, device_list, packs=packs)
    else:
        readiness_map = {}

    facts_by_device_id: dict[uuid.UUID, DeviceGroupFacts] = {}
    for device in device_list:
        readiness = readiness_map.get(device.id)
        facts_by_device_id[device.id] = build_device_group_facts(
            device,
            operational_state=op_map.get(device.id, DeviceOperationalState.offline),
            is_reserved=gating_owner_map.get(device.id) is not None,
            readiness_state=readiness.readiness_state if readiness is not None else "setup_required",
            static_group_keys=static_keys_map.get(device.id, frozenset()),
        )

    return evaluate_group_memberships(groups=groups, devices=device_list, facts_by_device_id=facts_by_device_id)
