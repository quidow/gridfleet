import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, and_, asc, desc, exists, false, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.models.appium_node import NodeState
from app.models.device import Device, DeviceAvailabilityStatus
from app.models.device_event import DeviceEventType
from app.models.device_reservation import DeviceReservation
from app.models.session import Session, SessionStatus
from app.models.test_run import TERMINAL_STATES, RunState, TestRun
from app.schemas.device import DeviceLifecyclePolicySummaryState
from app.schemas.run import DeviceRequirement, ReservedDeviceInfo, RunCreate, RunRead, SessionCounts
from app.services import device_health_summary, lifecycle_incident_service, maintenance_service, platform_label_service
from app.services.cursor_pagination import CursorPage, CursorToken, decode_cursor, encode_cursor
from app.services.device_readiness import is_ready_for_use_async
from app.services.event_bus import event_bus
from app.services.lifecycle_policy_state import (
    clear_backoff,
    set_action,
    write_state,
)
from app.services.lifecycle_policy_state import (
    state as policy_state,
)
from app.services.pack_platform_resolver import assert_runnable
from app.services.settings_service import settings_service

logger = logging.getLogger(__name__)


class _UnmetRequirementError(Exception):
    def __init__(self, requirement: DeviceRequirement, matched_count: int) -> None:
        self.requirement = requirement
        self.matched_count = matched_count
        super().__init__(f"{requirement.pack_id}/{requirement.platform_id}")


def _reserved_entry_is_excluded(entry: DeviceReservation) -> bool:
    return bool(entry.excluded)


def _older_than_cursor(cursor: CursorToken) -> ColumnElement[bool]:
    return or_(
        TestRun.created_at < cursor.timestamp,
        and_(TestRun.created_at == cursor.timestamp, TestRun.id < cursor.item_id),
    )


def _newer_than_cursor(cursor: CursorToken) -> ColumnElement[bool]:
    return or_(
        TestRun.created_at > cursor.timestamp,
        and_(TestRun.created_at == cursor.timestamp, TestRun.id > cursor.item_id),
    )


async def _has_run_rows(
    db: AsyncSession,
    stmt: Select[tuple[TestRun]],
    predicate: ColumnElement[bool],
) -> bool:
    result = await db.execute(stmt.where(predicate).order_by(None).limit(1))
    return result.scalar_one_or_none() is not None


def _reserved_entry_matches(entry: DeviceReservation, device_id: uuid.UUID | str) -> bool:
    return str(entry.device_id) == str(device_id)


def _reserved_entry_for_device(
    run: TestRun,
    device_id: uuid.UUID | str,
    *,
    active_only: bool = False,
) -> DeviceReservation | None:
    if not run.device_reservations:
        return None

    matching = [entry for entry in run.device_reservations if _reserved_entry_matches(entry, device_id)]
    if active_only:
        matching = [entry for entry in matching if entry.released_at is None]
    if not matching:
        return None
    return matching[-1]


def _generated_worker_id() -> str:
    return f"anonymous-{uuid.uuid4().hex[:8]}"


def _reservation_to_claim_response(entry: DeviceReservation) -> ReservedDeviceInfo:
    return ReservedDeviceInfo(
        device_id=str(entry.device_id),
        identity_value=entry.identity_value,
        connection_target=entry.connection_target,
        pack_id=entry.pack_id,
        platform_id=entry.platform_id,
        platform_label=entry.platform_label,
        os_version=entry.os_version,
        host_ip=entry.host_ip,
        excluded=entry.excluded,
        exclusion_reason=entry.exclusion_reason,
        excluded_at=entry.excluded_at.isoformat() if entry.excluded_at is not None else None,
        claimed_by=entry.claimed_by,
        claimed_at=entry.claimed_at.isoformat() if entry.claimed_at is not None else None,
    )


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
    stmt = (
        select(Device)
        .options(selectinload(Device.host))
        .where(Device.availability_status == DeviceAvailabilityStatus.available)
        .where(Device.pack_id == requirement.pack_id)
        .where(Device.platform_id == requirement.platform_id)
        .where(~active_reservation_exists)
        .order_by(Device.created_at, Device.id)
        .with_for_update(skip_locked=True)
    )
    if requirement.os_version:
        stmt = stmt.where(Device.os_version == requirement.os_version)
    if excluded_device_ids:
        stmt = stmt.where(Device.id.not_in(excluded_device_ids))

    result = await db.execute(stmt)
    raw_devices = result.scalars().all()
    devices: list[Device] = []
    for device in raw_devices:
        ready = await is_ready_for_use_async(db, device)
        health_allows_allocation = await device_health_summary.device_allows_allocation(db, device)
        if ready and health_allows_allocation:
            devices.append(device)

    if requirement.tags:
        filtered: list[Device] = []
        for device in devices:
            device_tags = device.tags or {}
            if all(device_tags.get(key) == value for key, value in requirement.tags.items()):
                filtered.append(device)
        devices = filtered

    return devices


def _build_device_info(device: Device, *, platform_label: str | None) -> ReservedDeviceInfo:
    host_ip = device.host.ip if device.host else None
    return ReservedDeviceInfo(
        device_id=str(device.id),
        identity_value=device.identity_value,
        connection_target=device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        platform_label=platform_label,
        os_version=device.os_version,
        host_ip=host_ip,
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

    label_map = await platform_label_service.load_platform_label_map(
        db,
        ((device.pack_id, device.platform_id) for device in all_matched),
    )

    device_infos: list[ReservedDeviceInfo] = []
    for device in all_matched:
        device.availability_status = DeviceAvailabilityStatus.reserved
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

    await event_bus.publish(
        "run.created",
        {
            "run_id": str(refreshed_run.id),
            "name": refreshed_run.name,
            "device_count": len(device_infos),
            "created_by": refreshed_run.created_by,
        },
    )

    return refreshed_run, device_infos


async def get_run(db: AsyncSession, run_id: uuid.UUID) -> TestRun | None:
    stmt = (
        select(TestRun)
        .where(TestRun.id == run_id)
        .options(selectinload(TestRun.device_reservations))
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_device_reservation_with_entry(
    db: AsyncSession,
    device_id: uuid.UUID,
) -> tuple[TestRun | None, DeviceReservation | None]:
    stmt = (
        select(DeviceReservation)
        .where(DeviceReservation.device_id == device_id, DeviceReservation.released_at.is_(None))
        .options(selectinload(DeviceReservation.run).selectinload(TestRun.device_reservations))
        .order_by(DeviceReservation.created_at.desc())
    )
    result = await db.execute(stmt)
    reservation = result.scalars().first()
    if reservation is None:
        return None, None
    return reservation.run, reservation


async def get_device_reservation_map(db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[uuid.UUID, TestRun]:
    if not device_ids:
        return {}

    stmt = (
        select(DeviceReservation)
        .where(DeviceReservation.device_id.in_(device_ids), DeviceReservation.released_at.is_(None))
        .options(selectinload(DeviceReservation.run).selectinload(TestRun.device_reservations))
    )
    result = await db.execute(stmt)
    reservation_map: dict[uuid.UUID, TestRun] = {}
    for reservation in result.scalars().all():
        reservation_map[reservation.device_id] = reservation.run
    return reservation_map


def get_reservation_entry_for_device(run: TestRun, device_id: uuid.UUID | str) -> DeviceReservation | None:
    return _reserved_entry_for_device(run, device_id, active_only=True)


def get_reservation_context_for_device(
    run: TestRun | None,
    device_id: uuid.UUID | str,
) -> tuple[TestRun | None, DeviceReservation | None]:
    if run is None:
        return None, None
    return run, get_reservation_entry_for_device(run, device_id)


async def get_device_reservation(db: AsyncSession, device_id: uuid.UUID) -> TestRun | None:
    reservation_map = await get_device_reservation_map(db, [device_id])
    return reservation_map.get(device_id)


async def exclude_device_from_run(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    reason: str,
    commit: bool = True,
) -> TestRun | None:
    run, entry = await get_device_reservation_with_entry(db, device_id)
    if run is None or entry is None:
        return None
    if _reserved_entry_is_excluded(entry) and entry.exclusion_reason == reason:
        return run

    entry.excluded = True
    entry.exclusion_reason = reason
    entry.excluded_at = datetime.now(UTC)
    if commit:
        await db.commit()
        run = await get_run(db, run.id)
    return run


async def restore_device_to_run(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    commit: bool = True,
) -> TestRun | None:
    run, entry = await get_device_reservation_with_entry(db, device_id)
    if run is None or entry is None:
        return None
    if not _reserved_entry_is_excluded(entry):
        return run

    entry.excluded = False
    entry.exclusion_reason = None
    entry.excluded_at = None
    if commit:
        await db.commit()
        run = await get_run(db, run.id)
    return run


def reservation_entry_is_excluded(entry: DeviceReservation | None) -> bool:
    return bool(entry and _reserved_entry_is_excluded(entry))


async def list_runs(
    db: AsyncSession,
    state: RunState | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
) -> tuple[list[TestRun], int]:
    stmt = select(TestRun).options(selectinload(TestRun.device_reservations))
    if state is not None:
        stmt = stmt.where(TestRun.state == state)
    if created_from is not None:
        stmt = stmt.where(TestRun.created_at >= created_from)
    if created_to is not None:
        stmt = stmt.where(TestRun.created_at <= created_to)

    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int((await db.execute(count_stmt)).scalar_one())

    reservation_count = (
        select(func.count(DeviceReservation.id)).where(DeviceReservation.run_id == TestRun.id).scalar_subquery()
    )
    duration_expr = func.coalesce(TestRun.completed_at, func.now()) - TestRun.created_at
    order_map = {
        "name": func.lower(TestRun.name),
        "state": TestRun.state,
        "devices": reservation_count,
        "created_by": func.lower(func.coalesce(TestRun.created_by, "")),
        "created_at": TestRun.created_at,
        "duration": duration_expr,
    }
    order_expr = order_map.get(sort_by, TestRun.created_at)
    order_fn = asc if sort_dir == "asc" else desc

    stmt = (
        stmt.order_by(
            order_fn(order_expr),
            order_fn(TestRun.created_at),
            order_fn(TestRun.id),
        )
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all()), total


async def list_runs_cursor(
    db: AsyncSession,
    state: RunState | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = 50,
    cursor: str | None = None,
    direction: str = "older",
) -> CursorPage[TestRun]:
    stmt = select(TestRun).options(selectinload(TestRun.device_reservations))
    if state is not None:
        stmt = stmt.where(TestRun.state == state)
    if created_from is not None:
        stmt = stmt.where(TestRun.created_at >= created_from)
    if created_to is not None:
        stmt = stmt.where(TestRun.created_at <= created_to)

    page_stmt = stmt
    cursor_token = decode_cursor(cursor) if cursor else None
    if cursor_token is not None:
        predicate = _newer_than_cursor(cursor_token) if direction == "newer" else _older_than_cursor(cursor_token)
        page_stmt = page_stmt.where(predicate)

    if direction == "newer":
        page_stmt = page_stmt.order_by(asc(TestRun.created_at), asc(TestRun.id))
    else:
        page_stmt = page_stmt.order_by(desc(TestRun.created_at), desc(TestRun.id))

    result = await db.execute(page_stmt.limit(limit))
    items = list(result.scalars().all())
    if direction == "newer":
        items.reverse()

    if not items:
        return CursorPage(items=[], limit=limit, next_cursor=None, prev_cursor=None)

    first_item = items[0]
    last_item = items[-1]
    has_newer = await _has_run_rows(db, stmt, _newer_than_cursor(CursorToken(first_item.created_at, first_item.id)))
    has_older = await _has_run_rows(db, stmt, _older_than_cursor(CursorToken(last_item.created_at, last_item.id)))
    return CursorPage(
        items=items,
        limit=limit,
        next_cursor=encode_cursor(last_item.created_at, last_item.id) if has_older else None,
        prev_cursor=encode_cursor(first_item.created_at, first_item.id) if has_newer else None,
    )


async def signal_ready(db: AsyncSession, run_id: uuid.UUID) -> TestRun:
    run = await get_run(db, run_id)
    if run is None:
        raise ValueError("Run not found")
    if run.state != RunState.preparing:
        raise ValueError(f"Cannot signal ready from state '{run.state.value}', expected 'preparing'")

    run.state = RunState.ready
    run.last_heartbeat = datetime.now(UTC)
    await db.commit()
    run = await get_run(db, run_id)
    assert run is not None

    await event_bus.publish("run.ready", {"run_id": str(run.id), "name": run.name})
    return run


async def signal_active(db: AsyncSession, run_id: uuid.UUID) -> TestRun:
    run = await get_run(db, run_id)
    if run is None:
        raise ValueError("Run not found")
    if run.state != RunState.ready:
        raise ValueError(f"Cannot signal active from state '{run.state.value}', expected 'ready'")

    now = datetime.now(UTC)
    run.state = RunState.active
    run.started_at = now
    run.last_heartbeat = now
    await db.commit()
    run = await get_run(db, run_id)
    assert run is not None

    await event_bus.publish("run.active", {"run_id": str(run.id), "name": run.name})
    return run


async def signal_active_for_device_session(db: AsyncSession, device_id: uuid.UUID) -> TestRun | None:
    run, entry = await get_device_reservation_with_entry(db, device_id)
    if run is None or run.state != RunState.ready or reservation_entry_is_excluded(entry):
        return None
    return await signal_active(db, run.id)


async def report_preparation_failure(
    db: AsyncSession,
    run_id: uuid.UUID,
    device_id: uuid.UUID,
    *,
    message: str,
    source: str = "ci_preparation",
) -> TestRun:
    run = await get_run(db, run_id)
    if run is None:
        raise ValueError("Run not found")
    if run.state in TERMINAL_STATES:
        raise ValueError(f"Cannot report preparation failure for terminal run '{run.state.value}'")

    entry = get_reservation_entry_for_device(run, device_id)
    if entry is None:
        raise ValueError("Device is not actively reserved by this run")

    reason = message.strip()
    if not reason:
        raise ValueError("Preparation failure message is required")

    stmt = select(Device).options(selectinload(Device.appium_node)).where(Device.id == device_id)
    result = await db.execute(stmt)
    device = result.scalar_one_or_none()
    if device is None:
        raise ValueError("Device not found")

    run = await exclude_device_from_run(db, device.id, reason=reason, commit=False)
    assert run is not None

    current_state = policy_state(device)
    current_state["last_failure_source"] = source
    current_state["last_failure_reason"] = reason
    current_state["stop_pending"] = False
    current_state["stop_pending_reason"] = None
    current_state["stop_pending_since"] = None
    current_state["recovery_suppressed_reason"] = "Device is in maintenance mode"
    clear_backoff(current_state)
    set_action(current_state, "ci_preparation_failed")
    write_state(device, current_state)

    await maintenance_service.enter_maintenance(db, device, drain=False, commit=False)
    await device_health_summary.update_device_checks(db, device, healthy=False, summary=reason)
    if device.appium_node is not None:
        await device_health_summary.update_node_state(
            db,
            device,
            running=device.appium_node.state == NodeState.running,
            state=device.appium_node.state.value,
        )

    await lifecycle_incident_service.record_lifecycle_incident(
        db,
        device,
        event_type=DeviceEventType.lifecycle_run_excluded,
        summary_state=DeviceLifecyclePolicySummaryState.excluded,
        reason=reason,
        detail=f"CI preparation failed, excluded the device from {run.name}, and placed it into maintenance",
        source=source,
        run_id=run.id,
        run_name=run.name,
    )
    await db.commit()

    refreshed_run = await get_run(db, run.id)
    assert refreshed_run is not None
    return refreshed_run


async def heartbeat(db: AsyncSession, run_id: uuid.UUID) -> TestRun:
    run = await get_run(db, run_id)
    if run is None:
        raise ValueError("Run not found")
    if run.state in TERMINAL_STATES:
        return run

    run.last_heartbeat = datetime.now(UTC)
    await db.commit()
    run = await get_run(db, run_id)
    assert run is not None
    return run


async def claim_device(
    db: AsyncSession,
    run_id: uuid.UUID,
    *,
    worker_id: str | None = None,
) -> ReservedDeviceInfo:
    run_result = await db.execute(select(TestRun).where(TestRun.id == run_id).with_for_update())
    run = run_result.scalar_one_or_none()
    if run is None:
        await db.rollback()
        raise ValueError("Run not found")
    if run.state in TERMINAL_STATES:
        state_value = run.state.value
        await db.rollback()
        raise ValueError(f"Cannot claim device from run in terminal state '{state_value}'")

    now = datetime.now(UTC)
    ttl_seconds = int(settings_service.get("reservations.claim_ttl_seconds"))
    stale_before = now - timedelta(seconds=ttl_seconds)

    await db.execute(
        update(DeviceReservation)
        .where(DeviceReservation.run_id == run_id)
        .where(DeviceReservation.released_at.is_(None))
        .where(DeviceReservation.claimed_by.is_not(None))
        .where(
            or_(
                DeviceReservation.claimed_at.is_(None),
                DeviceReservation.claimed_at <= stale_before,
            )
        )
        .values(claimed_by=None, claimed_at=None)
    )

    candidate_result = await db.execute(
        select(DeviceReservation)
        .where(DeviceReservation.run_id == run_id)
        .where(DeviceReservation.released_at.is_(None))
        .where(DeviceReservation.excluded == false())
        .where(DeviceReservation.claimed_by.is_(None))
        .order_by(DeviceReservation.created_at, DeviceReservation.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    candidate = candidate_result.scalar_one_or_none()
    if candidate is None:
        await db.rollback()
        raise ValueError("No unclaimed devices available in this run")

    candidate.claimed_by = worker_id or _generated_worker_id()
    candidate.claimed_at = now
    info = _reservation_to_claim_response(candidate)
    await db.commit()
    return info


async def release_claimed_device(
    db: AsyncSession,
    run_id: uuid.UUID,
    *,
    device_id: uuid.UUID,
    worker_id: str,
) -> None:
    run_result = await db.execute(select(TestRun).where(TestRun.id == run_id).with_for_update())
    run = run_result.scalar_one_or_none()
    if run is None:
        await db.rollback()
        raise ValueError("Run not found")

    result = await db.execute(
        select(DeviceReservation)
        .where(DeviceReservation.run_id == run_id)
        .where(DeviceReservation.device_id == device_id)
        .with_for_update()
        .limit(1)
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        await db.rollback()
        raise ValueError(f"Device {device_id} is not reserved by this run")

    if entry.released_at is not None:
        await db.rollback()
        return

    if entry.claimed_by is None:
        await db.rollback()
        raise ValueError(f"Device {device_id} is not claimed")
    if entry.claimed_by != worker_id:
        await db.rollback()
        raise ValueError(f"Device {device_id} is claimed by another worker")

    entry.claimed_by = None
    entry.claimed_at = None
    await db.commit()


async def complete_run(db: AsyncSession, run_id: uuid.UUID) -> TestRun:
    run = await get_run(db, run_id)
    if run is None:
        raise ValueError("Run not found")
    if run.state in TERMINAL_STATES:
        raise ValueError(f"Run is already in terminal state '{run.state.value}'")

    now = datetime.now(UTC)
    run.state = RunState.completed
    run.completed_at = now
    await db.commit()

    await _release_devices(db, run)
    run = await get_run(db, run_id)
    assert run is not None

    duration = None
    if run.started_at:
        duration = int((now - run.started_at).total_seconds())
    await event_bus.publish(
        "run.completed",
        {
            "run_id": str(run.id),
            "name": run.name,
            "duration": duration,
        },
    )
    return run


async def cancel_run(db: AsyncSession, run_id: uuid.UUID) -> TestRun:
    run = await get_run(db, run_id)
    if run is None:
        raise ValueError("Run not found")
    if run.state in TERMINAL_STATES:
        raise ValueError(f"Run is already in terminal state '{run.state.value}'")

    run.state = RunState.cancelled
    run.completed_at = datetime.now(UTC)
    await db.commit()

    await _release_devices(db, run)
    run = await get_run(db, run_id)
    assert run is not None

    await event_bus.publish(
        "run.cancelled",
        {
            "run_id": str(run.id),
            "name": run.name,
            "cancelled_by": "user",
        },
    )
    return run


async def force_release(db: AsyncSession, run_id: uuid.UUID) -> TestRun:
    run = await get_run(db, run_id)
    if run is None:
        raise ValueError("Run not found")

    run.state = RunState.cancelled
    run.error = "Force released by admin"
    run.completed_at = datetime.now(UTC)
    await db.commit()

    await _release_devices(db, run)
    run = await get_run(db, run_id)
    assert run is not None

    await event_bus.publish(
        "run.cancelled",
        {
            "run_id": str(run.id),
            "name": run.name,
            "cancelled_by": "admin (force release)",
        },
    )
    return run


async def expire_run(db: AsyncSession, run: TestRun, reason: str) -> None:
    """Expire a run due to heartbeat or TTL timeout. Called by the reaper."""

    run.state = RunState.expired
    run.error = reason
    run.completed_at = datetime.now(UTC)
    await db.commit()

    await _release_devices(db, run)

    await event_bus.publish(
        "run.expired",
        {
            "run_id": str(run.id),
            "name": run.name,
            "reason": reason,
        },
    )


async def _mark_running_sessions_released(db: AsyncSession, run: TestRun, released_at: datetime) -> None:
    stmt = select(Session).where(
        Session.run_id == run.id,
        Session.status == SessionStatus.running,
        Session.ended_at.is_(None),
    )
    result = await db.execute(stmt)
    sessions = result.scalars().all()
    if not sessions:
        return

    error_message = run.error if run.error else f"Run ended while session was still running ({run.state.value})"
    for session in sessions:
        session.status = SessionStatus.error
        session.ended_at = released_at
        session.error_type = "run_released"
        session.error_message = error_message


async def _device_has_running_session(db: AsyncSession, device_id: uuid.UUID) -> bool:
    stmt = (
        select(Session.id)
        .where(
            Session.device_id == device_id,
            Session.status == SessionStatus.running,
            Session.ended_at.is_(None),
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def _release_devices(db: AsyncSession, run: TestRun) -> None:
    """Release all active reservations for this run and restore device statuses."""

    active_reservations = [reservation for reservation in run.device_reservations if reservation.released_at is None]
    released_at = datetime.now(UTC)
    await _mark_running_sessions_released(db, run, released_at)

    if not active_reservations:
        await db.commit()
        return

    for reservation in active_reservations:
        reservation.released_at = released_at
        reservation.claimed_by = None
        reservation.claimed_at = None
        stmt = select(Device).where(Device.id == reservation.device_id)
        result = await db.execute(stmt)
        device = result.scalar_one_or_none()
        if device and device.availability_status in {DeviceAvailabilityStatus.reserved, DeviceAvailabilityStatus.busy}:
            old_availability_status = device.availability_status
            if old_availability_status == DeviceAvailabilityStatus.busy and await _device_has_running_session(
                db, device.id
            ):
                continue
            if await is_ready_for_use_async(db, device):
                device.availability_status = DeviceAvailabilityStatus.available
            else:
                device.availability_status = DeviceAvailabilityStatus.offline
            await event_bus.publish(
                "device.availability_changed",
                {
                    "device_id": str(device.id),
                    "device_name": device.name,
                    "old_availability_status": old_availability_status.value,
                    "new_availability_status": device.availability_status.value,
                    "reason": f"Run '{run.name}' ended ({run.state.value})",
                },
            )
    await db.commit()


async def fetch_session_counts(db: AsyncSession, run_ids: list[uuid.UUID]) -> dict[uuid.UUID, SessionCounts]:
    """Aggregate Session.status counts per run_id. Returns {} for empty input."""
    if not run_ids:
        return {}
    stmt = (
        select(Session.run_id, Session.status, func.count(Session.id))
        .where(Session.run_id.in_(run_ids))
        .group_by(Session.run_id, Session.status)
    )
    result = await db.execute(stmt)
    accum: dict[uuid.UUID, dict[str, int]] = {}
    for run_id, status, n in result.all():
        if run_id is None:
            continue
        status_value = status.value if isinstance(status, SessionStatus) else str(status)
        accum.setdefault(run_id, {})[status_value] = int(n)
    return {rid: SessionCounts.from_status_map(m) for rid, m in accum.items()}


def build_run_read(run: TestRun, counts: SessionCounts | None = None) -> RunRead:
    """Construct a RunRead from a TestRun ORM object plus optional session counts.

    Every RunRead-returning endpoint goes through this helper so `session_counts`
    stays consistent across list, detail, and lifecycle responses — even when
    counts are structurally guaranteed zero (e.g. signal_ready before any session
    has run). Consistency over micro-optimization.
    """
    return RunRead(
        id=run.id,
        name=run.name,
        state=run.state,
        requirements=run.requirements,
        ttl_minutes=run.ttl_minutes,
        heartbeat_timeout_sec=run.heartbeat_timeout_sec,
        reserved_devices=run.reserved_devices,
        error=run.error,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_by=run.created_by,
        last_heartbeat=run.last_heartbeat,
        session_counts=counts or SessionCounts(),
    )
