import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi import HTTPException
from sqlalchemy import Select, and_, asc, desc, exists, false, func, or_, select, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.models.device import Device, DeviceHold, DeviceOperationalState
from app.models.device_event import DeviceEventType
from app.models.device_reservation import DeviceReservation
from app.models.session import Session, SessionStatus
from app.models.test_run import TERMINAL_STATES, RunState, TestRun
from app.schemas.device import DeviceLifecyclePolicySummaryState
from app.schemas.run import DeviceRequirement, ReservedDeviceInfo, RunCreate, RunRead, SessionCounts, UnavailableInclude
from app.services import (
    capability_service,
    config_service,
    device_health,
    device_locking,
    grid_service,
    lifecycle_incident_service,
    lifecycle_policy,
    lifecycle_policy_actions,
    maintenance_service,
    platform_label_service,
    run_reservation_service,
)
from app.services.cursor_pagination import CursorPage, CursorToken, decode_cursor, encode_cursor
from app.services.device_event_service import record_event
from app.services.device_readiness import is_ready_for_use_async
from app.services.device_state import ready_operational_state, set_hold, set_operational_state
from app.services.event_bus import queue_event_for_session
from app.services.pack_platform_resolver import assert_runnable
from app.services.settings_service import settings_service

logger = logging.getLogger(__name__)

# Prefix written into exclusion_reason by the escalation path.
# Must match exactly what release_claimed_device_with_cooldown writes so that
# cooldown_escalated can be derived by a simple startswith() check.
_COOLDOWN_ESCALATION_REASON_PREFIX = "Exceeded cooldown threshold "


class _UnmetRequirementError(Exception):
    def __init__(self, requirement: DeviceRequirement, matched_count: int) -> None:
        self.requirement = requirement
        self.matched_count = matched_count
        super().__init__(f"{requirement.pack_id}/{requirement.platform_id}")


class NoClaimableDevicesError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        retry_after_sec: int,
        next_available_at: datetime | None,
    ) -> None:
        self.retry_after_sec = retry_after_sec
        self.next_available_at = next_available_at
        super().__init__(message)


def now_utc() -> datetime:
    return datetime.now(UTC)


def _cooldown_remaining_sec(excluded_until: datetime | None, *, now: datetime | None = None) -> int | None:
    if excluded_until is None:
        return None
    reference = now or now_utc()
    return max(0, int((excluded_until - reference).total_seconds()))


def _reserved_entry_is_excluded(entry: DeviceReservation) -> bool:
    if not entry.excluded:
        return False
    if entry.excluded_until is None:
        return True
    return entry.excluded_until > now_utc()


def _reservation_claimable_expr(now: datetime) -> ColumnElement[bool]:
    return or_(
        DeviceReservation.excluded == false(),
        and_(
            DeviceReservation.excluded.is_(True),
            DeviceReservation.excluded_until.is_not(None),
            DeviceReservation.excluded_until <= now,
        ),
    )


def _clear_expired_cooldown(entry: DeviceReservation, now: datetime) -> None:
    if entry.excluded and entry.excluded_until is not None and entry.excluded_until <= now:
        entry.excluded = False
        entry.exclusion_reason = None
        entry.excluded_at = None
        entry.excluded_until = None


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
    return cast("DeviceReservation", matching[-1])


def _generated_worker_id() -> str:
    return f"anonymous-{uuid.uuid4().hex[:8]}"


def parse_includes(value: str | None, *, allowed: set[str]) -> set[str]:
    if not value:
        return set()
    tokens = [token.strip() for token in value.split(",")]
    tokens = [token for token in tokens if token]
    unknown = sorted({token for token in tokens if token not in allowed})
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={"code": "unknown_include", "values": unknown},
        )
    return set(tokens)


async def hydrate_reserved_device_info(
    db: AsyncSession,
    info: ReservedDeviceInfo,
    device: Device,
    *,
    includes: set[str],
) -> None:
    """Attach optional config + live capabilities to a single ReservedDeviceInfo.

    Mutates ``info.config``/``live_capabilities``/``unavailable_includes`` in place.
    Caller must pass a ``Device`` with the ``appium_node`` relationship loaded.
    Never raises on missing data — sets ``None`` and records the reason.
    """
    unavailable: list[UnavailableInclude] = []

    if "config" in includes:
        try:
            info.config = await config_service.get_device_config(db, device, keys=None)
        except Exception as exc:
            info.config = None
            unavailable.append(UnavailableInclude(include="config", reason=type(exc).__name__))

    if "capabilities" in includes:
        try:
            info.live_capabilities = await capability_service.get_device_capabilities(db, device)
        except Exception as exc:
            info.live_capabilities = None
            unavailable.append(UnavailableInclude(include="capabilities", reason=type(exc).__name__))

    if "test_data" in includes:
        info.test_data = device.test_data or {}

    info.unavailable_includes = unavailable or None


async def hydrate_reserved_device_infos(
    db: AsyncSession,
    pairs: list[tuple[ReservedDeviceInfo, Device]],
    *,
    includes: set[str],
) -> None:
    """Batched variant for reserve."""
    if not includes or not pairs:
        return
    for info, device in pairs:
        await hydrate_reserved_device_info(db, info, device, includes=includes)


def mark_reserved_device_info_includes_unavailable(
    info: ReservedDeviceInfo,
    *,
    includes: set[str],
    reason: str,
) -> None:
    if "config" in includes:
        info.config = None
    if "capabilities" in includes:
        info.live_capabilities = None
    if "test_data" in includes:
        info.test_data = None

    unavailable = list(info.unavailable_includes or [])
    existing = {item.include for item in unavailable}
    for include in ("config", "capabilities", "test_data"):
        if include in includes and include not in existing:
            unavailable.append(UnavailableInclude(include=include, reason=reason))
    info.unavailable_includes = unavailable or None


def _reservation_to_claim_response(entry: DeviceReservation) -> ReservedDeviceInfo:
    device = entry.device
    return ReservedDeviceInfo(
        device_id=str(entry.device_id),
        identity_value=entry.identity_value,
        name=device.name if device is not None else None,
        connection_target=entry.connection_target,
        pack_id=entry.pack_id,
        platform_id=entry.platform_id,
        platform_label=entry.platform_label,
        os_version=entry.os_version,
        host_ip=entry.host_ip,
        device_type=(device.device_type.value if device is not None and device.device_type is not None else None),
        connection_type=(
            device.connection_type.value if device is not None and device.connection_type is not None else None
        ),
        manufacturer=device.manufacturer if device is not None else None,
        model=device.model if device is not None else None,
        excluded=entry.excluded,
        exclusion_reason=entry.exclusion_reason,
        excluded_at=entry.excluded_at.isoformat() if entry.excluded_at is not None else None,
        excluded_until=entry.excluded_until.isoformat() if entry.excluded_until is not None else None,
        cooldown_remaining_sec=_cooldown_remaining_sec(entry.excluded_until),
        cooldown_count=entry.cooldown_count,
        cooldown_escalated=bool(
            entry.exclusion_reason and entry.exclusion_reason.startswith(_COOLDOWN_ESCALATION_REASON_PREFIX)
        ),
        claimed_by=entry.claimed_by,
        claimed_at=entry.claimed_at.isoformat() if entry.claimed_at is not None else None,
    )


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
        .where(Device.operational_state == DeviceOperationalState.available, Device.hold.is_(None))
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
        .where(Device.id.in_(candidate_ids))
        .where(Device.operational_state == DeviceOperationalState.available, Device.hold.is_(None))
        .where(~active_reservation_exists)
        .order_by(Device.created_at, Device.id)
        .with_for_update(skip_locked=True)
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


async def get_run(db: AsyncSession, run_id: uuid.UUID) -> TestRun | None:
    stmt = (
        select(TestRun)
        .where(TestRun.id == run_id)
        .options(selectinload(TestRun.device_reservations).selectinload(DeviceReservation.device))
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _get_run_for_update(db: AsyncSession, run_id: uuid.UUID) -> TestRun | None:
    stmt = (
        select(TestRun)
        .where(TestRun.id == run_id)
        .options(selectinload(TestRun.device_reservations).selectinload(DeviceReservation.device))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_device_reservation_with_entry(
    db: AsyncSession,
    device_id: uuid.UUID,
) -> tuple[TestRun | None, DeviceReservation | None]:
    return await run_reservation_service.get_device_reservation_with_entry(db, device_id)


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
    entry.excluded_until = None
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
    if entry.excluded_until is not None and entry.excluded_until > now_utc():
        return run
    if not _reserved_entry_is_excluded(entry):
        return run

    entry.excluded = False
    entry.exclusion_reason = None
    entry.excluded_at = None
    entry.excluded_until = None
    if commit:
        await db.commit()
        run = await get_run(db, run.id)
    return run


def reservation_entry_is_excluded(entry: DeviceReservation | None) -> bool:
    return run_reservation_service.reservation_entry_is_excluded(entry)


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
    stmt = select(TestRun).options(selectinload(TestRun.device_reservations).selectinload(DeviceReservation.device))
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
    stmt = select(TestRun).options(selectinload(TestRun.device_reservations).selectinload(DeviceReservation.device))
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
    run = await _get_run_for_update(db, run_id)
    if run is None:
        raise ValueError("Run not found")
    if run.state != RunState.preparing:
        raise ValueError(f"Cannot signal ready from state '{run.state.value}', expected 'preparing'")

    run.state = RunState.ready
    run.last_heartbeat = datetime.now(UTC)
    queue_event_for_session(db, "run.ready", {"run_id": str(run.id), "name": run.name})
    await db.commit()
    run = await get_run(db, run_id)
    assert run is not None
    return run


async def signal_active(db: AsyncSession, run_id: uuid.UUID) -> TestRun:
    await _signal_active_no_commit(db, run_id)
    await db.commit()
    run = await get_run(db, run_id)
    assert run is not None
    return run


async def _signal_active_no_commit(db: AsyncSession, run_id: uuid.UUID) -> TestRun:
    run = await _get_run_for_update(db, run_id)
    if run is None:
        raise ValueError("Run not found")
    if run.state != RunState.ready:
        raise ValueError(f"Cannot signal active from state '{run.state.value}', expected 'ready'")

    now = datetime.now(UTC)
    run.state = RunState.active
    run.started_at = now
    run.last_heartbeat = now
    queue_event_for_session(db, "run.active", {"run_id": str(run.id), "name": run.name})
    return run


async def signal_active_for_device_session(db: AsyncSession, device_id: uuid.UUID) -> TestRun | None:
    run, entry = await get_device_reservation_with_entry(db, device_id)
    if run is None or run.state != RunState.ready or reservation_entry_is_excluded(entry):
        return None
    return await signal_active(db, run.id)


async def signal_active_for_device_session_no_commit(db: AsyncSession, device_id: uuid.UUID) -> TestRun | None:
    run, entry = await get_device_reservation_with_entry(db, device_id)
    if run is None or run.state != RunState.ready or reservation_entry_is_excluded(entry):
        return None
    return await _signal_active_no_commit(db, run.id)


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

    try:
        device = await device_locking.lock_device(db, device_id, load_sessions=False)
    except NoResultFound:
        raise ValueError("Device not found") from None

    run = await exclude_device_from_run(db, device.id, reason=reason, commit=False)
    assert run is not None

    await lifecycle_policy_actions.record_ci_preparation_failed(
        db,
        device,
        reason=reason,
        source=source,
    )

    await maintenance_service.enter_maintenance(db, device, drain=False, commit=False, allow_reserved=True)
    await device_health.update_device_checks(db, device, healthy=False, summary=reason)

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
    run = await _get_run_for_update(db, run_id)
    if run is None:
        raise ValueError("Run not found")
    if run.state in TERMINAL_STATES:
        await db.commit()
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

    now = now_utc()
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
        .options(selectinload(DeviceReservation.device))
        .where(DeviceReservation.run_id == run_id)
        .where(DeviceReservation.released_at.is_(None))
        .where(_reservation_claimable_expr(now))
        .where(DeviceReservation.claimed_by.is_(None))
        .order_by(DeviceReservation.created_at, DeviceReservation.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    candidate = candidate_result.scalar_one_or_none()
    if candidate is None:
        retry_after_sec = int(settings_service.get("general.claim_default_retry_after_sec"))
        next_available_at = await _next_claimable_cooldown_at(db, run_id, now)
        await db.rollback()
        raise NoClaimableDevicesError(
            "No unclaimed devices available in this run",
            retry_after_sec=retry_after_sec,
            next_available_at=next_available_at,
        )

    _clear_expired_cooldown(candidate, now)
    candidate.claimed_by = worker_id or _generated_worker_id()
    candidate.claimed_at = now
    info = _reservation_to_claim_response(candidate)
    await db.commit()
    return info


async def _next_claimable_cooldown_at(db: AsyncSession, run_id: uuid.UUID, now: datetime) -> datetime | None:
    result = await db.execute(
        select(func.min(DeviceReservation.excluded_until))
        .where(DeviceReservation.run_id == run_id)
        .where(DeviceReservation.released_at.is_(None))
        .where(DeviceReservation.excluded_until.is_not(None))
        .where(DeviceReservation.excluded_until > now)
    )
    return result.scalar_one_or_none()


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


async def release_claimed_device_with_cooldown(
    db: AsyncSession,
    run_id: uuid.UUID,
    *,
    device_id: uuid.UUID,
    worker_id: str,
    reason: str,
    ttl_seconds: int,
) -> tuple[
    ReservedDeviceInfo,
    DeviceOperationalState,
    DeviceHold | None,
    datetime | None,
    int,  # cooldown_count after this call
    bool,  # escalated to maintenance?
    int,  # threshold (for response payload)
]:
    max_ttl = int(settings_service.get("general.device_cooldown_max_sec"))
    if ttl_seconds > max_ttl:
        raise ValueError(f"ttl_seconds must be <= {max_ttl}")
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError("Cooldown reason is required")

    threshold = int(settings_service.get("general.device_cooldown_escalation_threshold"))

    excluded_until: datetime | None = None
    escalate = False
    cooldown_count_after = 0
    # These are assigned in exactly one of the two branches below; the
    # initializer placeholders prevent mypy possibly-unbound warnings.
    reservation_payload: ReservedDeviceInfo
    next_operational_state: DeviceOperationalState
    device_hold: DeviceHold | None

    # Tx 1: locked-write phase — validates, increments cooldown_count, clears the
    # claim, and fully completes the cooldown branch when not escalating.
    async with db.begin():
        run_result = await db.execute(select(TestRun).where(TestRun.id == run_id).with_for_update())
        run = run_result.scalar_one_or_none()
        if run is None:
            raise ValueError("Run not found")
        if run.state in TERMINAL_STATES:
            raise ValueError(f"Cannot release device from terminal run '{run.state.value}'")

        try:
            device = await device_locking.lock_device(db, device_id, load_sessions=True)
        except NoResultFound:
            raise ValueError("Device not found") from None

        result = await db.execute(
            select(DeviceReservation)
            .options(selectinload(DeviceReservation.device))
            .where(DeviceReservation.run_id == run_id)
            .where(DeviceReservation.device_id == device_id)
            .where(DeviceReservation.released_at.is_(None))
            .with_for_update()
            .limit(1)
        )
        entry = result.scalar_one_or_none()
        if entry is None:
            raise ValueError(f"Device {device_id} is not actively reserved by this run")
        if entry.claimed_by is None:
            raise ValueError(f"Device {device_id} is not claimed")
        if entry.claimed_by != worker_id:
            raise ValueError(f"Device {device_id} is claimed by another worker")

        entry.cooldown_count += 1
        entry.claimed_by = None
        entry.claimed_at = None

        cooldown_count_after = entry.cooldown_count
        escalate = threshold > 0 and entry.cooldown_count >= threshold

        if escalate:
            # Mark the row unclaimable atomically inside Tx1 so that concurrent
            # claim_device calls (which filter excluded==False) cannot re-pick
            # this device during the gap between Tx1 commit and the escalation
            # phase that runs outside the transaction.
            entry.excluded = True
            entry.excluded_at = now_utc()
            entry.excluded_until = None
            entry.exclusion_reason = (
                f"{_COOLDOWN_ESCALATION_REASON_PREFIX}({cooldown_count_after}/{threshold}): {clean_reason}"
            )

        if not escalate:
            excluded_at = now_utc()
            excluded_until = excluded_at + timedelta(seconds=ttl_seconds)
            entry.excluded = True
            entry.exclusion_reason = clean_reason
            entry.excluded_at = excluded_at
            entry.excluded_until = excluded_until

            next_operational_state = await ready_operational_state(db, device)
            await set_operational_state(device, next_operational_state, reason=f"cooldown:{clean_reason}")

            await lifecycle_incident_service.record_lifecycle_incident(
                db,
                device,
                event_type=DeviceEventType.lifecycle_run_cooldown_set,
                summary_state=DeviceLifecyclePolicySummaryState.excluded,
                reason=clean_reason,
                detail=f"Worker {worker_id} released the device with a {ttl_seconds}s cooldown",
                source="testkit",
                run_id=run.id,
                run_name=run.name,
                ttl_seconds=ttl_seconds,
                worker_id=worker_id,
                expires_at=excluded_until,
            )
            reservation_payload = _reservation_to_claim_response(entry)
            device_hold = device.hold

    # Tx 1 has committed (or raised). Cooldown branch is fully done at this point.

    if escalate:
        # Tx 2+: escalation path — mirrors complete_auto_stop which also calls
        # exclude_run_if_needed followed by db.commit() without an enclosing
        # db.begin() block. exclude_run_if_needed internally calls
        # enter_maintenance which calls stop_node; stop_node commits
        # unconditionally when the node is running, so we must not nest this
        # inside an outer async with db.begin(): block.
        #
        # escalation_reason was already written into entry.exclusion_reason in Tx1
        # (Fix 2 — atomic exclusion), so we derive it from there rather than
        # rebuilding the string, ensuring both are identical.
        escalation_reason = entry.exclusion_reason or (
            f"{_COOLDOWN_ESCALATION_REASON_PREFIX}({cooldown_count_after}/{threshold}): {clean_reason}"
        )

        # Before re-locking the device for escalation work, verify the device's
        # CURRENT active reservation still belongs to this run.  A concurrent
        # complete_run / cancel_run / force_release may have released the old
        # reservation (setting released_at) between Tx1 committing and here, and
        # a new run could have reserved the same device — in that case we MUST NOT
        # put the new run's device into maintenance.
        async with db.begin():
            _current_run, current_entry = await run_reservation_service.get_device_reservation_with_entry(db, device_id)
            if current_entry is None or str(current_entry.run_id) != str(run_id):
                logger.warning(
                    "device.cooldown.escalation.skipped_after_reassignment",
                    extra={
                        "device_id": str(device_id),
                        "original_run_id": str(run_id),
                        "current_run_id": str(current_entry.run_id) if current_entry else None,
                    },
                )
                # Tx1 already incremented cooldown_count and excluded the old
                # reservation; the device just won't enter maintenance because it
                # is no longer ours.  Return the escalation response for the OLD
                # reservation — its exclusion IS permanent, the device simply
                # wasn't put into maintenance on this path.
                return (
                    _reservation_to_claim_response(entry),
                    entry.device.operational_state if entry.device is not None else DeviceOperationalState.offline,
                    None,  # no maintenance hold was applied
                    None,  # excluded_until (permanent exclusion has no TTL)
                    cooldown_count_after,
                    True,  # escalated — the OLD reservation IS excluded permanently
                    threshold,
                )
        # Reassignment check passed: active reservation still belongs to this run.

        # Re-lock device for a fresh row after Tx 1 committed.
        device = await device_locking.lock_device(db, device_id, load_sessions=True)
        run_for_event = await db.execute(select(TestRun).where(TestRun.id == run_id))
        run_obj = run_for_event.scalar_one()

        await record_event(
            db,
            device.id,
            DeviceEventType.lifecycle_run_cooldown_escalated,
            {
                "cooldown_count": cooldown_count_after,
                "threshold": threshold,
                "reason": clean_reason,
                "worker_id": worker_id,
                "run_id": str(run_obj.id),
                "run_name": run_obj.name,
            },
        )

        # Because we pre-set excluded=True in Tx1 (Fix 2), exclude_run_if_needed
        # will see was_excluded=True and skip the lifecycle incident and
        # enter_maintenance calls that normally follow inside it.  We therefore
        # invoke those two steps explicitly here so the device always ends up in
        # maintenance and the lifecycle_run_excluded event always fires.
        await lifecycle_policy_actions.exclude_run_if_needed(
            db,
            device,
            reason=escalation_reason,
            source="testkit",
        )
        # Fire the lifecycle incident and enter maintenance explicitly — these are
        # skipped by exclude_run_if_needed when the row was already excluded (Tx1).
        # Reuse run_obj fetched above (same transaction / session).
        await lifecycle_incident_service.record_lifecycle_incident(
            db,
            device,
            DeviceEventType.lifecycle_run_excluded,
            summary_state=DeviceLifecyclePolicySummaryState.excluded,
            reason=escalation_reason,
            detail=f"Excluded from {run_obj.name}",
            source="testkit",
            run_id=run_obj.id,
            run_name=run_obj.name,
        )
        if "appium_node" not in device.__dict__:
            await db.refresh(device, ["appium_node"])
        await maintenance_service.enter_maintenance(db, device, drain=False, commit=False, allow_reserved=True)
        # enter_maintenance + exclude_run_if_needed leave mutations in the session; commit them.
        await db.commit()
        # Refresh device so the response reflects post-maintenance state.
        # enter_maintenance may have committed mid-flight via stop_node and
        # re-set the hold, so we need fresh values here.
        await db.refresh(device, ["operational_state", "hold"])
        # Re-fetch entry because exclude_device_from_run may have mutated
        # excluded_at and exclusion_reason further. Use scalar_one_or_none so
        # that a concurrent complete_run/cancel_run/force_release that sets
        # released_at between our db.commit() and this re-fetch does not raise
        # NoResultFound (Fix 3). Fall back to the in-memory entry from Tx1.
        result = await db.execute(
            select(DeviceReservation)
            .options(selectinload(DeviceReservation.device))
            .where(DeviceReservation.run_id == run_id)
            .where(DeviceReservation.device_id == device_id)
            .where(DeviceReservation.released_at.is_(None))
            .limit(1)
        )
        fresh_entry = result.scalar_one_or_none()
        if fresh_entry is not None:
            entry = fresh_entry  # reflects exclude_device_from_run's mutations
        # else: entry retains the in-memory state from Tx1 (excluded=True,
        # exclusion_reason set, cooldown_count incremented) which is correct.
        reservation_payload = _reservation_to_claim_response(entry)
        next_operational_state = device.operational_state
        device_hold = device.hold

    if escalate:
        logger.info("device.cooldown.escalated")
    else:
        logger.info("device.cooldown.set")
    return (
        reservation_payload,
        next_operational_state,
        device_hold,
        excluded_until,
        cooldown_count_after,
        escalate,
        threshold,
    )


async def complete_run(db: AsyncSession, run_id: uuid.UUID) -> TestRun:
    run = await _get_run_for_update(db, run_id)
    if run is None:
        raise ValueError("Run not found")
    if run.state in TERMINAL_STATES:
        raise ValueError(f"Run is already in terminal state '{run.state.value}'")

    now = datetime.now(UTC)
    run.state = RunState.completed
    run.completed_at = now
    cleanup_ids = await _release_devices(db, run, commit=False, terminate_grid_sessions=False)

    duration = None
    if run.started_at:
        duration = int((now - run.started_at).total_seconds())
    queue_event_for_session(
        db,
        "run.completed",
        {
            "run_id": str(run.id),
            "name": run.name,
            "duration": duration,
        },
    )
    await db.commit()
    await _complete_deferred_stops_post_commit(db, cleanup_ids)
    run = await get_run(db, run_id)
    assert run is not None
    return run


async def cancel_run(db: AsyncSession, run_id: uuid.UUID) -> TestRun:
    run = await _get_run_for_update(db, run_id)
    if run is None:
        raise ValueError("Run not found")
    if run.state in TERMINAL_STATES:
        raise ValueError(f"Run is already in terminal state '{run.state.value}'")

    run.state = RunState.cancelled
    run.completed_at = datetime.now(UTC)
    cleanup_ids = await _release_devices(db, run, commit=False, terminate_grid_sessions=True)
    queue_event_for_session(
        db,
        "run.cancelled",
        {
            "run_id": str(run.id),
            "name": run.name,
            "cancelled_by": "user",
        },
    )
    await db.commit()
    await _complete_deferred_stops_post_commit(db, cleanup_ids)
    run = await get_run(db, run_id)
    assert run is not None
    return run


async def force_release(db: AsyncSession, run_id: uuid.UUID) -> TestRun:
    run = await _get_run_for_update(db, run_id)
    if run is None:
        raise ValueError("Run not found")

    run.state = RunState.cancelled
    run.error = "Force released by admin"
    run.completed_at = datetime.now(UTC)
    cleanup_ids = await _release_devices(db, run, commit=False, terminate_grid_sessions=True)
    queue_event_for_session(
        db,
        "run.cancelled",
        {
            "run_id": str(run.id),
            "name": run.name,
            "cancelled_by": "admin (force release)",
        },
    )
    await db.commit()
    await _complete_deferred_stops_post_commit(db, cleanup_ids)
    run = await get_run(db, run_id)
    assert run is not None
    return run


async def expire_run(db: AsyncSession, run: TestRun, reason: str) -> None:
    """Expire a run due to heartbeat or TTL timeout. Called by the reaper."""

    locked_run = await _get_run_for_update(db, run.id)
    if locked_run is None:
        return
    if locked_run.state in TERMINAL_STATES:
        await db.commit()
        return

    locked_run.state = RunState.expired
    locked_run.error = reason
    locked_run.completed_at = datetime.now(UTC)
    cleanup_ids = await _release_devices(db, locked_run, commit=False, terminate_grid_sessions=True)

    queue_event_for_session(
        db,
        "run.expired",
        {
            "run_id": str(locked_run.id),
            "name": locked_run.name,
            "reason": reason,
        },
    )
    await db.commit()
    await _complete_deferred_stops_post_commit(db, cleanup_ids)


async def _mark_running_sessions_released(
    db: AsyncSession,
    run: TestRun,
    released_at: datetime,
    *,
    terminate_grid_sessions: bool,
) -> None:
    if not terminate_grid_sessions:
        # complete_run path: session lifecycle is owned by the testkit/operator.
        # Leaving running rows untouched keeps _device_has_running_session honest
        # so devices with live Grid sessions are not freed under the run.
        return

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
        if not await grid_service.terminate_grid_session(session.session_id):
            logger.warning(
                "Leaving session %s running because Grid deletion failed during run %s release",
                session.session_id,
                run.id,
            )
            continue

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


async def _release_devices(
    db: AsyncSession,
    run: TestRun,
    *,
    commit: bool = True,
    terminate_grid_sessions: bool = False,
) -> list[uuid.UUID]:
    """Release all active reservations for this run and restore device statuses.

    Returns the device IDs that need a follow-up
    ``complete_deferred_stop_if_session_ended`` pass. The caller MUST run
    ``_complete_deferred_stops_post_commit`` after the encompassing run-state
    commit; the lifecycle helper commits internally (via
    ``handle_node_crash``) and must not be invoked while the run-state
    transaction is still open, otherwise a partial commit can land on disk if
    a later step in the same call raises.
    """

    active_reservations = [reservation for reservation in run.device_reservations if reservation.released_at is None]
    released_at = datetime.now(UTC)
    await _mark_running_sessions_released(
        db,
        run,
        released_at,
        terminate_grid_sessions=terminate_grid_sessions,
    )

    if not active_reservations:
        if commit:
            await db.commit()
        return []

    device_ids = sorted({reservation.device_id for reservation in active_reservations})
    locked_devices = {device.id: device for device in await device_locking.lock_devices(db, device_ids)}
    devices_pending_lifecycle_cleanup: list[uuid.UUID] = []

    for reservation in active_reservations:
        reservation.released_at = released_at
        reservation.claimed_by = None
        reservation.claimed_at = None
        device = locked_devices.get(reservation.device_id)
        if device is None:
            logger.warning(
                "Reservation %s references missing device %s; skipping availability restore",
                reservation.id,
                reservation.device_id,
            )
            continue
        if device.hold == DeviceHold.maintenance:
            devices_pending_lifecycle_cleanup.append(device.id)
            continue
        if device.hold != DeviceHold.reserved and device.operational_state != DeviceOperationalState.busy:
            devices_pending_lifecycle_cleanup.append(device.id)
            continue
        if device.hold == DeviceHold.reserved:
            await set_hold(device, None, reason=f"Run '{run.name}' ended ({run.state.value})")
        if device.operational_state == DeviceOperationalState.busy and await _device_has_running_session(db, device.id):
            devices_pending_lifecycle_cleanup.append(device.id)
            continue
        await set_operational_state(
            device,
            await ready_operational_state(db, device),
            reason=f"Run '{run.name}' ended ({run.state.value})",
        )
        devices_pending_lifecycle_cleanup.append(device.id)
    if commit:
        await db.commit()
    return devices_pending_lifecycle_cleanup


async def _complete_deferred_stops_post_commit(db: AsyncSession, device_ids: list[uuid.UUID]) -> None:
    """Run ``complete_deferred_stop_if_session_ended`` for each device after
    the caller's run-state commit landed. Skips devices that vanished in the
    meantime."""
    for device_id in device_ids:
        device = await db.get(Device, device_id)
        if device is None:
            continue
        await lifecycle_policy.complete_deferred_stop_if_session_ended(db, device)


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
