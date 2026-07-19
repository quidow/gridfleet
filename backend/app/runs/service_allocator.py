from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.agent_comm.node_poke import poke_node_refresh
from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services.node_viability import device_node_is_viable, node_viable_predicate
from app.core.errors import (
    PackDisabledError,
    PackDrainingError,
    PackUnavailableError,
    PlatformRemovedError,
)
from app.core.timeutil import now_utc
from app.devices.models import Device, DeviceOperationalState, DeviceReservation
from app.devices.services import attention as device_attention
from app.devices.services import health as device_health
from app.devices.services.claims import active_reservation_exists, reservation_active
from app.devices.services.group_membership import (
    DeviceGroupFacts,
    GroupMembershipIndex,
    evaluate_group_memberships,
    load_group_membership_index,
    load_groups_by_keys,
)
from app.devices.services.group_membership import (
    _load_static_group_keys_by_device_id as load_static_group_keys_by_device_id,
)
from app.devices.services.intent import IntentService
from app.devices.services.platform_label import load_platform_label_map
from app.devices.services.readiness import (
    _assess_device_with_pack,
    assess_devices_async,
    is_ready_for_use_async,
    load_packs_by_ids,
)
from app.devices.services.service import UnknownGroupKeysError
from app.devices.services.state import derive_operational_states, is_available_sql
from app.hosts import service_hardware_telemetry as hardware_telemetry
from app.packs.models import DriverPack, DriverPackRelease, PackState
from app.packs.services.release_ordering import selected_release
from app.runs.models import RunState, TestRun
from app.runs.schemas import (
    DeviceRequirement,
    ReservedDeviceInfo,
    RunCreate,
)
from app.runs.service_reservation import get_run

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher


# A run-create match can come up short purely because Stage-3's
# ``SELECT ... FOR UPDATE SKIP LOCKED`` skipped a candidate whose row was
# momentarily locked by a background reconcile loop (the device is fully
# allocatable). Re-match a bounded number of times before surfacing the
# shortfall. Lab measurement (2026-06-10) put loop lock holds at a ~600ms
# median (max ~2.3s) before the connectivity sweep committed per device;
# 5 x 250ms outlasts the residual windows while adding at most ~1s before a
# genuine shortfall surfaces.
_MATCH_RETRY_ATTEMPTS = 5
_MATCH_RETRY_BACKOFF_SEC = 0.25
_RESTART_WINDOW_FALLBACK_SEC = 120


class _UnmetRequirementError(Exception):
    def __init__(self, requirement: DeviceRequirement, matched_count: int) -> None:
        self.requirement = requirement
        self.matched_count = matched_count
        super().__init__(f"{requirement.pack_id}/{requirement.platform_id}")


async def _readiness_for_match(db: AsyncSession, device: Device) -> bool:
    return await is_ready_for_use_async(db, device) and device_health.device_allows_allocation(device)


async def _find_matching_devices(
    db: AsyncSession,
    requirement: DeviceRequirement,
    *,
    restart_window_sec: int = _RESTART_WINDOW_FALLBACK_SEC,
    excluded_device_ids: set[uuid.UUID] | None = None,
) -> list[Device]:
    now = now_utc()
    candidate_stmt = (
        select(Device)
        .options(selectinload(Device.host), selectinload(Device.appium_node))
        .outerjoin(AppiumNode, AppiumNode.device_id == Device.id)
        .where(is_available_sql(now=now))
        .where(Device.review_required.is_(False))
        .where(node_viable_predicate(now=now, restart_window_sec=restart_window_sec))
        .where(Device.pack_id == requirement.pack_id)
        .where(Device.platform_id == requirement.platform_id)
        .where(~active_reservation_exists())
        .order_by(Device.created_at, Device.id)
    )
    if requirement.os_version:
        candidate_stmt = candidate_stmt.where(Device.os_version == requirement.os_version)
    if excluded_device_ids:
        candidate_stmt = candidate_stmt.where(Device.id.not_in(excluded_device_ids))

    candidates = list((await db.execute(candidate_stmt)).scalars().all())

    ready_candidates: list[Device] = [device for device in candidates if await _readiness_for_match(db, device)]

    if not ready_candidates:
        return []

    candidate_ids = [device.id for device in ready_candidates]
    locked_stmt = (
        select(Device)
        .options(selectinload(Device.host), selectinload(Device.appium_node))
        .outerjoin(AppiumNode, AppiumNode.device_id == Device.id)
        .where(Device.id.in_(candidate_ids))
        .where(is_available_sql(now=now))
        .where(Device.review_required.is_(False))
        .where(node_viable_predicate(now=now, restart_window_sec=restart_window_sec))
        .where(~active_reservation_exists())
        .order_by(Device.created_at, Device.id)
        .with_for_update(of=Device, skip_locked=True)
        .execution_options(populate_existing=True)
    )
    locked_rows = list((await db.execute(locked_stmt)).scalars().all())
    locked_ready_by_id: dict[uuid.UUID, Device] = {}
    for locked_device in locked_rows:
        if await _readiness_for_match(db, locked_device):
            locked_ready_by_id[locked_device.id] = locked_device
    return [locked_ready_by_id[device.id] for device in ready_candidates if device.id in locked_ready_by_id]


async def _classify_shortfall_gates(
    db: AsyncSession,
    requirement: DeviceRequirement,
    devices: list[Device],
    reserved_run_by_device: dict[uuid.UUID, uuid.UUID],
    *,
    restart_window_sec: int,
    settings: SettingsReader,
) -> tuple[Counter[str], Counter[str], set[uuid.UUID]]:
    """Bucket each device by the first allocator gate it fails.

    Classification mirrors the gate order of the batch allocator
    (``_batch_select_devices``). Returns ``(state_counts, gate_counts,
    blocking_runs)``. Loads one device/reservation/readiness/group batch and
    consumes pure maps per device instead of querying per device.
    """
    state_counts: Counter[str] = Counter()
    gate_counts: Counter[str] = Counter()
    blocking_runs: set[uuid.UUID] = set()
    if not devices:
        return state_counts, gate_counts, blocking_runs

    now = now_utc()

    # One batch: operational states, readiness, group membership index (when
    # the requirement pins groups). The pack catalog is loaded once and reused
    # by both the operational-state batch and the readiness batch.
    pack_catalog = await load_packs_by_ids(db, {device.pack_id for device in devices if device.pack_id})
    op_states = await derive_operational_states(db, devices, now=now, packs=pack_catalog)
    readiness_map = await assess_devices_async(db, devices, packs=pack_catalog)

    group_index: GroupMembershipIndex | None = None
    if requirement.groups:
        groups = await load_groups_by_keys(db, requirement.groups)
        group_index = await load_group_membership_index(
            db,
            groups=groups,
            devices=devices,
            settings=settings,
            pack_catalog=pack_catalog,
            operational_states=op_states,
            reservation_owner_by_device_id=reserved_run_by_device,
        )

    for device in devices:
        operational_state = op_states.get(device.id, DeviceOperationalState.offline)
        if operational_state != DeviceOperationalState.available:
            state_counts[operational_state.value] += 1
        elif device.review_required:
            gate_counts["review"] += 1
        elif not device_node_is_viable(device, now=now, restart_window_sec=restart_window_sec):
            gate_counts["node"] += 1
        elif device.id in reserved_run_by_device:
            gate_counts["reserved"] += 1
            blocking_runs.add(reserved_run_by_device[device.id])
        elif (
            requirement.groups
            and group_index is not None
            and not group_index.matches_all(device.id, requirement.groups)
        ):
            gate_counts["groups"] += 1
        elif not (
            readiness_map.get(device.id) is not None
            and readiness_map[device.id].readiness_state == "verified"
            and device_health.device_allows_allocation(device)
        ):
            gate_counts["readiness"] += 1
        else:
            gate_counts["eligible"] += 1
    return state_counts, gate_counts, blocking_runs


def _format_shortfall_parts(
    device_count: int,
    state_counts: Counter[str],
    gate_counts: Counter[str],
    blocking_runs: set[uuid.UUID],
) -> str:
    """Render the per-gate counters into the actionable 409 message."""
    parts: list[str] = []
    if gate_counts["reserved"]:
        named_runs = ", ".join(sorted(str(run_id) for run_id in blocking_runs)[:3])
        plural = "s" if len(blocking_runs) > 1 else ""
        parts.append(f"{gate_counts['reserved']} held by active reservation (run{plural} {named_runs})")
    for state, count in sorted(state_counts.items()):
        parts.append(f"{count} in state {state}")
    if gate_counts["node"]:
        parts.append(f"{gate_counts['node']} with Appium node not viable (stopped or mid-transition)")
    if gate_counts["review"]:
        parts.append(f"{gate_counts['review']} flagged review_required")
    if gate_counts["groups"]:
        parts.append(f"{gate_counts['groups']} not matching requested groups")
    if gate_counts["readiness"]:
        parts.append(f"{gate_counts['readiness']} not ready or health-blocked")
    if gate_counts["eligible"]:
        parts.append(f"{gate_counts['eligible']} eligible at re-check (transient contention; retry)")
    plural = "s" if device_count != 1 else ""
    return f"{device_count} candidate device{plural}: " + ", ".join(parts)


async def _describe_requirement_shortfall(
    db: AsyncSession,
    requirement: DeviceRequirement,
    *,
    settings: SettingsReader,
) -> str:
    """Per-gate breakdown of why the requirement's candidates were excluded.

    Two allocator gates — active reservations and Appium-node viability — are
    orthogonal to ``operational_state``, so a shortfall they cause is invisible
    on the device list ("the dashboard says the devices are available"). Name
    the failing gate, and for reservations the blocking run, so the 409 is
    actionable without DB spelunking. Classification mirrors the gate order of
    the batch allocator (``_batch_select_devices``). Runs on the rolled-back
    session after matching already failed — plain SELECTs, no locks.
    """
    stmt = (
        select(Device)
        .options(selectinload(Device.appium_node), selectinload(Device.host))
        .where(Device.pack_id == requirement.pack_id)
        .where(Device.platform_id == requirement.platform_id)
    )
    if requirement.os_version:
        stmt = stmt.where(Device.os_version == requirement.os_version)
    devices = list((await db.execute(stmt)).scalars().all())
    if not devices:
        return "no devices are configured for this pack/platform"

    reserved_run_by_device: dict[uuid.UUID, uuid.UUID] = dict(
        (
            await db.execute(
                select(DeviceReservation.device_id, DeviceReservation.run_id).where(
                    DeviceReservation.device_id.in_([device.id for device in devices]),
                    reservation_active(),
                )
            )
        )
        .tuples()
        .all()
    )

    state_counts, gate_counts, blocking_runs = await _classify_shortfall_gates(
        db,
        requirement,
        devices,
        reserved_run_by_device,
        restart_window_sec=_RESTART_WINDOW_FALLBACK_SEC,
        settings=settings,
    )
    return _format_shortfall_parts(len(devices), state_counts, gate_counts, blocking_runs)


def _build_device_info(device: Device, *, platform_label: str | None) -> ReservedDeviceInfo:
    host_ip = device.host.ip if device.host else None
    return ReservedDeviceInfo(
        device_id=str(device.id),
        identity_value=device.identity_value,
        name=device.name,
        connection_target=device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        platform_label=platform_label,
        os_version=device.os_version,
        host_ip=host_ip,
        device_type=device.device_type.value if device.device_type is not None else None,
        connection_type=device.connection_type.value if device.connection_type is not None else None,
        manufacturer=device.manufacturer,
        model=device.model,
        excluded=False,
    )


def _minimum_required_count(requirement: DeviceRequirement) -> int:
    if requirement.allocation == "all_available":
        assert requirement.min_count is not None
        return requirement.min_count
    assert requirement.count is not None
    return requirement.count


def _select_matching_devices(requirement: DeviceRequirement, available: list[Device]) -> list[Device]:
    if requirement.allocation == "all_available":
        return available
    assert requirement.count is not None
    return available[: requirement.count]


def _format_requirement_count(requirement: DeviceRequirement) -> str:
    if requirement.allocation == "all_available":
        return f"allocation=all_available, min_count={requirement.min_count}"
    return f"count={requirement.count}"


def _resolve_pack_platform_pairs(requirements: list[DeviceRequirement]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str]] = []
    for req in requirements:
        key = (req.pack_id, req.platform_id)
        if key not in seen:
            seen.add(key)
            pairs.append(key)
    return pairs


def _device_matches_requirement_static(
    device: Device,
    req: DeviceRequirement,
    *,
    readiness_lookup: dict[uuid.UUID, bool],
    group_index: GroupMembershipIndex | None,
) -> bool:
    if device.pack_id != req.pack_id or device.platform_id != req.platform_id:
        return False
    if req.os_version and device.os_version != req.os_version:
        return False
    if req.groups and group_index is not None and not group_index.matches_all(device.id, req.groups):
        return False
    return readiness_lookup.get(device.id, False)


async def _batch_select_devices(  # noqa: PLR0912, PLR0915
    db: AsyncSession,
    requirements: list[DeviceRequirement],
    *,
    restart_window_sec: int,
    settings: SettingsReader,
) -> list[list[Device]]:
    """One batch pass: load every fact the run-allocator needs in a bounded number
    of reads, then select devices per requirement in request order excluding
    already-selected ids. Returns one device list per requirement (parallel to
    ``requirements``).

    Read budget (candidate-selection phase, before the run INSERT):

    1. recursive CTE load of group definitions (only when any requirement pins groups),
    2. one batched pack-row lock + state check across all distinct pack_ids,
    3. one batched stereotype-template / pack load for all (pack_id, platform_id) pairs,
    4. one candidate-devices SELECT joined to host + appium_node across every
       requirement's (pack_id, platform_id) pair,
    5. one batched readiness assessment over the candidate set (reuses the pack
       catalog from step 3),
    6. one static-group-keys read (folded into the membership index loader) when
       any requirement pins groups,
    7. one locked recheck SELECT ... FOR UPDATE OF devices SKIP LOCKED over the
       selected device ids.

    Steps 1-7 are constant in the candidate and requirement counts: the candidate
    SELECT projects every matching row in one statement, the readiness assessment
    reuses the batched pack catalog, and the locked recheck is a single id-list
    query.
    """
    # Step 1: validate group keys once, before any device lock. Raises
    # UnknownGroupKeysError -> HTTP 422 if any key is missing.
    all_group_keys: set[str] = set()
    for req in requirements:
        all_group_keys.update(req.groups)
    groups = await load_groups_by_keys(db, all_group_keys) if all_group_keys else []
    loaded_group_keys = {group.key for group in groups}
    missing_groups = sorted(all_group_keys - loaded_group_keys)
    if missing_groups:
        raise UnknownGroupKeysError(missing_groups)

    # Step 2: batch-lock all distinct pack rows once (FOR SHARE) and validate
    # state. Replaces the per-requirement assert_runnable(..., pack_lock=True).
    pack_ids = sorted({req.pack_id for req in requirements})
    pack_rows = (
        (
            await db.execute(
                select(DriverPack)
                .where(DriverPack.id.in_(pack_ids))
                .with_for_update(read=True)
                .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms))
            )
        )
        .scalars()
        .all()
    )
    pack_by_id = {pack.id: pack for pack in pack_rows}
    for req in requirements:
        pack = pack_by_id.get(req.pack_id)
        if pack is None:
            raise PackUnavailableError(req.pack_id)
        if pack.state == PackState.disabled:
            raise PackDisabledError(req.pack_id)
        if pack.state == PackState.draining:
            raise PackDrainingError(req.pack_id)
        if pack.state != PackState.enabled:
            raise PackDisabledError(req.pack_id)
        release = selected_release(pack.releases, pack.current_release)
        platform = (
            next((row for row in release.platforms if row.manifest_platform_id == req.platform_id), None)
            if release is not None
            else None
        )
        if release is None or platform is None:
            raise PlatformRemovedError(req.pack_id, req.platform_id)

    # Step 3: the pack catalog loaded in step 2 already carries releases + platforms
    # for every pack; reuse it for readiness assessment (no extra read).
    pack_catalog = pack_by_id

    # Step 4: one candidate-devices SELECT across every (pack_id, platform_id)
    # pair. Joined to host + appium_node, gated by availability / review / node
    # viability / no active reservation, ordered by created_at for deterministic
    # FIFO selection.
    now = now_utc()
    pairs = _resolve_pack_platform_pairs(requirements)
    pair_clauses = or_(*[(Device.pack_id == pid) & (Device.platform_id == plid) for pid, plid in pairs])
    candidate_stmt = (
        select(Device)
        .options(selectinload(Device.host), selectinload(Device.appium_node))
        .outerjoin(AppiumNode, AppiumNode.device_id == Device.id)
        .where(is_available_sql(now=now))
        .where(Device.review_required.is_(False))
        .where(node_viable_predicate(now=now, restart_window_sec=restart_window_sec))
        .where(~active_reservation_exists())
        .where(pair_clauses)
        .order_by(Device.created_at, Device.id)
    )
    candidates = list((await db.execute(candidate_stmt)).scalars().all())

    # Step 5: one batched readiness assessment over the candidate set, reusing
    # the pack catalog. Maps device_id -> bool (ready and health-allowing).
    readiness_map = await assess_devices_async(db, candidates, packs=pack_catalog)
    readiness_lookup: dict[uuid.UUID, bool] = {
        device.id: (
            readiness_map[device.id].readiness_state == "verified" and device_health.device_allows_allocation(device)
        )
        for device in candidates
    }

    # Step 6: build the group membership index once over the candidate set when
    # any requirement pins groups. Static-only groups fold to a single
    # static-group-keys read; dynamic groups add the reservation/operational-state
    # batch reads (still constant in candidate count).
    group_index: GroupMembershipIndex | None = None
    if groups:
        group_index = await load_group_membership_index(
            db, groups=groups, devices=candidates, settings=settings, pack_catalog=pack_catalog
        )

    # Step 7a: in-memory selection per requirement in request order, excluding
    # already-selected device ids. The locked recheck follows.
    selected_ids: set[uuid.UUID] = set()
    per_requirement_candidates: list[list[Device]] = []
    device_to_requirement_idx: dict[uuid.UUID, int] = {}
    for req_idx, req in enumerate(requirements):
        picked: list[Device] = []
        for device in candidates:
            if device.id in selected_ids:
                continue
            if _device_matches_requirement_static(
                device, req, readiness_lookup=readiness_lookup, group_index=group_index
            ):
                picked.append(device)
                device_to_requirement_idx[device.id] = req_idx
                if req.allocation != "all_available":
                    assert req.count is not None
                    if len(picked) >= req.count:
                        break
        per_requirement_candidates.append(picked)
        selected_ids.update(device.id for device in picked)

    # Step 7b: one locked recheck SELECT ... FOR UPDATE OF devices SKIP LOCKED
    # over the full selected-id set. Revalidates availability, review, node
    # viability, and reservation absence under the lock; drops any device that
    # lost a gate or was skipped. Readiness is re-evaluated synchronously against
    # the freshly locked row + the already-loaded pack catalog (no new read).
    # Group membership is re-resolved against a fresh index rebuilt from a
    # static-group-keys reload over the locked ids (the Device-row lock does not
    # serialize DeviceGroupMembership edits).
    all_selected_ids = list(selected_ids)
    if not all_selected_ids:
        return per_requirement_candidates
    locked_stmt = (
        select(Device)
        .options(selectinload(Device.host), selectinload(Device.appium_node))
        .outerjoin(AppiumNode, AppiumNode.device_id == Device.id)
        .where(Device.id.in_(all_selected_ids))
        .where(is_available_sql(now=now))
        .where(Device.review_required.is_(False))
        .where(node_viable_predicate(now=now, restart_window_sec=restart_window_sec))
        .where(~active_reservation_exists())
        .with_for_update(of=Device, skip_locked=True)
        .execution_options(populate_existing=True)
    )
    locked_rows = {row.id: row for row in (await db.execute(locked_stmt)).scalars().all()}
    # Reload the static-group-key map for the locked-id set and rebuild a fresh
    # membership index under the lock: the Device-row lock does not serialize
    # concurrent DeviceGroupMembership edits, so the step-6 index is a pre-lock
    # snapshot. The reload is one joined SELECT over the locked ids (constant-
    # size at this point — the picked candidates, not the whole eligible batch).
    locked_group_index: GroupMembershipIndex | None = None
    if groups:
        locked_devices_list = list(locked_rows.values())
        locked_static_keys = await load_static_group_keys_by_device_id(db, list(locked_rows))
        locked_facts: dict[uuid.UUID, DeviceGroupFacts] = {}
        for locked_device in locked_devices_list:
            pack = pack_catalog.get(locked_device.pack_id)
            readiness_state = _assess_device_with_pack(locked_device, pack).readiness_state
            hardware_telemetry_state = hardware_telemetry.hardware_telemetry_state_for_device(
                locked_device, settings=settings
            )
            hardware_health_status = hardware_telemetry.current_hardware_health_status(locked_device)
            locked_facts[locked_device.id] = DeviceGroupFacts(
                operational_state=DeviceOperationalState.available,
                is_reserved=False,
                readiness_state=readiness_state,
                hardware_telemetry_state=hardware_telemetry_state,
                needs_attention=device_attention.compute_needs_attention(
                    DeviceOperationalState.available,
                    readiness_state,
                    hardware_health_status=hardware_health_status,
                    review_required=False,
                ),
                static_group_keys=locked_static_keys.get(locked_device.id, frozenset()),
            )
        locked_group_index = evaluate_group_memberships(
            groups=groups, devices=locked_devices_list, facts_by_device_id=locked_facts
        )
    locked_ready: dict[uuid.UUID, Device] = {}
    for device in locked_rows.values():
        pack = pack_catalog.get(device.pack_id)
        readiness = _assess_device_with_pack(device, pack)
        if readiness.readiness_state != "verified":
            continue
        if not device_health.device_allows_allocation(device):
            continue
        req_index = device_to_requirement_idx.get(device.id)
        if req_index is not None:
            req = requirements[req_index]
            if (
                req.groups
                and locked_group_index is not None
                and not locked_group_index.matches_all(device.id, req.groups)
            ):
                continue
        locked_ready[device.id] = device

    reconciled: list[list[Device]] = []
    for picked in per_requirement_candidates:
        kept = [locked_ready[device.id] for device in picked if device.id in locked_ready]
        reconciled.append(kept)
    return reconciled


class RunAllocatorService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        settings: SettingsReader,
        circuit_breaker: CircuitBreakerProtocol,
        pool: AgentHttpPool | None = None,
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._circuit_breaker = circuit_breaker
        self._pool = pool

    def _restart_window_sec(self) -> int:
        value = self._settings.get("appium_reconciler.restart_window_sec")
        return int(value) if value is not None else _RESTART_WINDOW_FALLBACK_SEC

    async def create_run(self, db: AsyncSession, data: RunCreate) -> tuple[TestRun, list[ReservedDeviceInfo]]:
        """Create a test run reservation. Returns (run, reserved_device_infos)."""

        ttl_minutes, heartbeat_timeout_sec = self._resolve_run_options(data)

        attempt = 0
        while True:
            try:
                run, device_infos = await self._attempt_create_run(
                    db,
                    data,
                    ttl_minutes=ttl_minutes,
                    heartbeat_timeout_sec=heartbeat_timeout_sec,
                )
                self._publisher.queue_for_session(
                    db,
                    "run.created",
                    {
                        "run_id": str(run.id),
                        "name": run.name,
                        "device_count": len(device_infos),
                        "created_by": run.created_by,
                    },
                )
                await db.commit()
                break
            except _UnmetRequirementError as exc:
                # The shortfall may be a transient false negative: Stage-3's
                # ``SELECT ... FOR UPDATE SKIP LOCKED`` drops a candidate whose
                # row a background reconcile loop holds for its (sub-second)
                # commit window. Roll back and re-match before surfacing.
                await db.rollback()
                attempt += 1
                if attempt >= _MATCH_RETRY_ATTEMPTS:
                    shortfall = await _describe_requirement_shortfall(db, exc.requirement, settings=self._settings)
                    raise ValueError(
                        "Not enough devices for requirement: "
                        f"pack_id={exc.requirement.pack_id}, "
                        f"platform_id={exc.requirement.platform_id}, "
                        f"os_version={exc.requirement.os_version}, "
                        f"{_format_requirement_count(exc.requirement)} "
                        f"(matched {exc.matched_count} eligible devices right now; {shortfall}). "
                        "Check /api/availability for current platform capacity or retry later."
                    ) from exc
                await asyncio.sleep(_MATCH_RETRY_BACKOFF_SEC)
            except Exception:
                await db.rollback()
                raise

        await self._deliver_routing_reconfigures(db, device_infos)

        refreshed_run = await get_run(db, run.id)
        assert refreshed_run is not None
        return refreshed_run, device_infos

    async def _deliver_routing_reconfigures(self, db: AsyncSession, device_infos: list[ReservedDeviceInfo]) -> None:
        """Wake each reserved device's agent inline so it re-pulls its desired
        state (carrying the new run id) without waiting for the next poll.
        """
        # ponytail: sequential, not gathered — each poke queries the shared
        # AsyncSession, which is not safe for concurrent use. A down host costs
        # N * NODE_POKE_TIMEOUT_SEC here; dedup by host (or per-task sessions)
        # only if that edge case ever matters.
        for info in device_infos:
            await poke_node_refresh(
                db,
                uuid.UUID(info.device_id),
                settings=self._settings,
                circuit_breaker=self._circuit_breaker,
                pool=self._pool,
                publisher=self._publisher,
            )

    def _resolve_run_options(self, data: RunCreate) -> tuple[int, int]:
        ttl_minutes = data.ttl_minutes
        if ttl_minutes is None:
            ttl_minutes = self._settings.get("reservations.default_ttl_minutes")

        max_ttl_minutes = self._settings.get("reservations.max_ttl_minutes")
        if ttl_minutes > max_ttl_minutes:
            raise ValueError(f"TTL {ttl_minutes} exceeds maximum allowed TTL of {max_ttl_minutes} minutes")

        heartbeat_timeout_sec = data.heartbeat_timeout_sec
        if heartbeat_timeout_sec is None:
            heartbeat_timeout_sec = self._settings.get("reservations.default_heartbeat_timeout_sec")

        return ttl_minutes, heartbeat_timeout_sec

    async def _attempt_create_run(
        self,
        db: AsyncSession,
        data: RunCreate,
        *,
        ttl_minutes: int,
        heartbeat_timeout_sec: int,
    ) -> tuple[TestRun, list[ReservedDeviceInfo]]:
        now = now_utc()
        selection = await _batch_select_devices(
            db,
            data.requirements,
            restart_window_sec=self._restart_window_sec(),
            settings=self._settings,
        )
        all_matched: list[Device] = []
        for req, devices in zip(data.requirements, selection, strict=True):
            required_count = _minimum_required_count(req)
            if len(devices) < required_count:
                raise _UnmetRequirementError(req, len(devices))
            all_matched.extend(_select_matching_devices(req, devices))

        label_map = await load_platform_label_map(
            db,
            ((device.pack_id, device.platform_id) for device in all_matched),
        )

        device_infos: list[ReservedDeviceInfo] = [
            _build_device_info(
                device,
                platform_label=label_map.get((device.pack_id, device.platform_id)),
            )
            for device in all_matched
        ]

        run = TestRun(
            name=data.name,
            state=RunState.preparing,
            requirements=[r.model_dump(exclude_none=True) for r in data.requirements],
            ttl_minutes=ttl_minutes,
            heartbeat_timeout_sec=heartbeat_timeout_sec,
            last_heartbeat=now,
            created_by=data.created_by,
        )
        db.add(run)
        await db.flush()

        reservations = [
            DeviceReservation(
                run=run,
                device_id=uuid.UUID(info.device_id),
                identity_value=info.identity_value,
                connection_target=info.connection_target,
                pack_id=info.pack_id,
                platform_id=info.platform_id,
                platform_label=info.platform_label,
                os_version=info.os_version,
                host_ip=info.host_ip,
                excluded=info.excluded,
                exclusion_reason=info.exclusion_reason,
                excluded_at=(
                    datetime.fromisoformat(info.excluded_at.replace("Z", "+00:00")) if info.excluded_at else None
                ),
            )
            for info in device_infos
        ]
        db.add_all(reservations)
        await db.flush()

        # Reconcile each allocated device so its node picks up the run: grid-routing
        # intent now synthesized from the reservation row just written.
        for device in all_matched:
            await IntentService(db).reconcile_now(device.id, publisher=self._publisher)

        return run, device_infos
