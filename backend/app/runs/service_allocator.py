import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.appium_nodes.models import AppiumNode
from app.devices.models import Device, DeviceHold, DeviceOperationalState, DeviceReservation
from app.devices.services import health as device_health
from app.devices.services.intent import register_intents_and_reconcile
from app.devices.services.intent_types import (
    GRID_ROUTING,
    PRIORITY_RUN_ROUTING,
    IntentRegistration,
)
from app.devices.services.platform_label import load_platform_label_map
from app.devices.services.readiness import is_ready_for_use_async
from app.devices.services.state import set_hold
from app.events import queue_event_for_session
from app.packs.services.platform_resolver import assert_runnable
from app.runs.models import RunState, TestRun
from app.runs.schemas import (
    DeviceRequirement,
    ReservedDeviceInfo,
    RunCreate,
)
from app.runs.service_reservation import get_run
from app.settings import settings_service


class _UnmetRequirementError(Exception):
    def __init__(self, requirement: DeviceRequirement, matched_count: int) -> None:
        self.requirement = requirement
        self.matched_count = matched_count
        super().__init__(f"{requirement.pack_id}/{requirement.platform_id}")


async def _readiness_for_match(db: AsyncSession, device: Device) -> bool:
    return await is_ready_for_use_async(db, device) and device_health.device_allows_allocation(device)


def _device_matches_requirement_tags(device: Device, tags: dict[str, str] | None) -> bool:
    if not tags:
        return True
    device_tags = device.tags or {}
    return all(device_tags.get(key) == value for key, value in tags.items())


async def _find_matching_devices(
    db: AsyncSession,
    requirement: DeviceRequirement,
    excluded_device_ids: set[uuid.UUID] | None = None,
) -> list[Device]:
    active_reservation_exists = exists(
        select(DeviceReservation.id).where(
            DeviceReservation.device_id == Device.id,
            DeviceReservation.released_at.is_(None),
        )
    )
    candidate_stmt = (
        select(Device)
        .options(selectinload(Device.host), selectinload(Device.appium_node))
        .outerjoin(AppiumNode, AppiumNode.device_id == Device.id)
        .where(Device.operational_state == DeviceOperationalState.available, Device.hold.is_(None))
        .where(
            or_(
                AppiumNode.id.is_(None),
                and_(
                    AppiumNode.pid.is_not(None),
                    AppiumNode.active_connection_target.is_not(None),
                    AppiumNode.transition_token.is_(None),
                ),
            )
        )
        .where(Device.pack_id == requirement.pack_id)
        .where(Device.platform_id == requirement.platform_id)
        .where(~active_reservation_exists)
        .order_by(Device.created_at, Device.id)
    )
    if requirement.os_version:
        candidate_stmt = candidate_stmt.where(Device.os_version == requirement.os_version)
    if excluded_device_ids:
        candidate_stmt = candidate_stmt.where(Device.id.not_in(excluded_device_ids))

    candidates = list((await db.execute(candidate_stmt)).scalars().all())
    candidates = [device for device in candidates if _device_matches_requirement_tags(device, requirement.tags)]

    ready_candidates: list[Device] = []
    for device in candidates:
        if await _readiness_for_match(db, device):
            ready_candidates.append(device)

    if not ready_candidates:
        return []

    candidate_ids = [device.id for device in ready_candidates]
    locked_stmt = (
        select(Device)
        .options(selectinload(Device.host), selectinload(Device.appium_node))
        .outerjoin(AppiumNode, AppiumNode.device_id == Device.id)
        .where(Device.id.in_(candidate_ids))
        .where(Device.operational_state == DeviceOperationalState.available, Device.hold.is_(None))
        .where(
            or_(
                AppiumNode.id.is_(None),
                and_(
                    AppiumNode.pid.is_not(None),
                    AppiumNode.active_connection_target.is_not(None),
                    AppiumNode.transition_token.is_(None),
                ),
            )
        )
        .where(~active_reservation_exists)
        .order_by(Device.created_at, Device.id)
        .with_for_update(of=Device, skip_locked=True)
        .execution_options(populate_existing=True)
    )
    locked_rows = list((await db.execute(locked_stmt)).scalars().all())
    locked_ready_by_id: dict[uuid.UUID, Device] = {}
    for locked_device in locked_rows:
        if not _device_matches_requirement_tags(locked_device, requirement.tags):
            continue
        if await _readiness_for_match(db, locked_device):
            locked_ready_by_id[locked_device.id] = locked_device
    return [locked_ready_by_id[device.id] for device in ready_candidates if device.id in locked_ready_by_id]


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
        tags=device.tags or None,
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


def _resolve_run_options(data: RunCreate) -> tuple[int, int]:
    ttl_minutes = data.ttl_minutes
    if ttl_minutes is None:
        ttl_minutes = settings_service.get("reservations.default_ttl_minutes")

    max_ttl_minutes = settings_service.get("reservations.max_ttl_minutes")
    if ttl_minutes > max_ttl_minutes:
        raise ValueError(f"TTL {ttl_minutes} exceeds maximum allowed TTL of {max_ttl_minutes} minutes")

    heartbeat_timeout_sec = data.heartbeat_timeout_sec
    if heartbeat_timeout_sec is None:
        heartbeat_timeout_sec = settings_service.get("reservations.default_heartbeat_timeout_sec")

    return ttl_minutes, heartbeat_timeout_sec


async def _attempt_create_run(
    db: AsyncSession,
    data: RunCreate,
    *,
    ttl_minutes: int,
    heartbeat_timeout_sec: int,
) -> tuple[TestRun, list[ReservedDeviceInfo]]:
    now = datetime.now(UTC)
    all_matched: list[Device] = []

    for req in data.requirements:
        await assert_runnable(db, pack_id=req.pack_id, platform_id=req.platform_id)
        already_ids = {device.id for device in all_matched}
        available = await _find_matching_devices(db, req, excluded_device_ids=already_ids)
        required_count = _minimum_required_count(req)
        if len(available) < required_count:
            raise _UnmetRequirementError(req, len(available))
        all_matched.extend(_select_matching_devices(req, available))

    label_map = await load_platform_label_map(
        db,
        ((device.pack_id, device.platform_id) for device in all_matched),
    )

    device_infos: list[ReservedDeviceInfo] = []
    for device in all_matched:
        await set_hold(
            device,
            DeviceHold.reserved,
            reason=f"Reserved for run '{data.name}'",
        )
        device_infos.append(
            _build_device_info(
                device,
                platform_label=label_map.get((device.pack_id, device.platform_id)),
            )
        )

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
            excluded_at=(datetime.fromisoformat(info.excluded_at.replace("Z", "+00:00")) if info.excluded_at else None),
        )
        for info in device_infos
    ]
    db.add_all(reservations)
    await db.flush()

    for device in all_matched:
        await register_intents_and_reconcile(
            db,
            device_id=device.id,
            intents=[
                IntentRegistration(
                    source=f"run:{run.id}",
                    axis=GRID_ROUTING,
                    run_id=run.id,
                    payload={"accepting_new_sessions": True, "priority": PRIORITY_RUN_ROUTING},
                )
            ],
            reason=f"reserved for run {run.id}",
        )

    return run, device_infos


async def create_run(db: AsyncSession, data: RunCreate) -> tuple[TestRun, list[ReservedDeviceInfo]]:
    """Create a test run reservation. Returns (run, reserved_device_infos)."""

    ttl_minutes, heartbeat_timeout_sec = _resolve_run_options(data)

    try:
        run, device_infos = await _attempt_create_run(
            db,
            data,
            ttl_minutes=ttl_minutes,
            heartbeat_timeout_sec=heartbeat_timeout_sec,
        )
        queue_event_for_session(
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
    except _UnmetRequirementError as exc:
        await db.rollback()
        raise ValueError(
            "Not enough devices for requirement: "
            f"pack_id={exc.requirement.pack_id}, "
            f"platform_id={exc.requirement.platform_id}, "
            f"os_version={exc.requirement.os_version}, "
            f"{_format_requirement_count(exc.requirement)} "
            f"(matched {exc.matched_count} eligible devices right now). "
            "Check /api/availability for current platform capacity or retry later."
        ) from exc
    except Exception:
        await db.rollback()
        raise

    refreshed_run = await get_run(db, run.id)
    assert refreshed_run is not None
    return refreshed_run, device_infos
