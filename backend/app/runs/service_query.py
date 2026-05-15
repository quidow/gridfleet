import uuid
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import Select, and_, asc, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

import app.devices.services.capability as capability_service
from app.core.pagination import CursorPage, CursorToken, decode_cursor, encode_cursor
from app.devices.models import Device, DeviceReservation
from app.runs.models import RunState, TestRun
from app.runs.schemas import (
    ReservedDeviceInfo,
    RunRead,
    SessionCounts,
    UnavailableInclude,
)
from app.runs.service_reservation import get_run as get_run
from app.sessions.models import Session, SessionStatus
from app.settings import service_config as config_service


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
        except Exception as exc:  # noqa: BLE001 — best-effort include; report unavailable instead of failing the run fetch
            info.config = None
            unavailable.append(UnavailableInclude(include="config", reason=type(exc).__name__))

    if "capabilities" in includes:
        try:
            info.live_capabilities = await capability_service.get_device_capabilities(db, device)
        except Exception as exc:  # noqa: BLE001 — best-effort include; report unavailable instead of failing the run fetch
            info.live_capabilities = None
            unavailable.append(UnavailableInclude(include="capabilities", reason=type(exc).__name__))

    if "test_data" in includes:
        try:
            info.test_data = device.test_data or {}
        except Exception as exc:  # noqa: BLE001 — best-effort include; report unavailable instead of failing the run fetch
            info.test_data = None
            unavailable.append(UnavailableInclude(include="test_data", reason=type(exc).__name__))

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
