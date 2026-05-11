from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, or_, select, text

from app.database import async_session
from app.models.analytics_capacity_snapshot import AnalyticsCapacitySnapshot
from app.models.appium_node import AppiumNode
from app.models.device import Device, DeviceHold, DeviceOperationalState
from app.models.host import Host, HostStatus
from app.models.session import Session, SessionStatus
from app.observability import get_logger, observe_background_loop
from app.schemas.analytics import FleetCapacityTimeline, FleetCapacityTimelinePoint
from app.services import grid_service
from app.services.session_filters import exclude_non_test_sessions
from app.services.settings_service import settings_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)
LOOP_NAME = "fleet_capacity_collector"
DEFAULT_BUCKET_MINUTES = 1
MAX_BUCKET_MINUTES = 1440

CAPACITY_ERROR_PATTERNS: tuple[str, ...] = (
    "no matching capability",
    "no matching capabilities",
    "matching capability not found",
    "matching capabilities not found",
    "cannot find matching capabilities",
    "could not find matching capabilities",
    "no nodes support",
    "no node supports",
    "no available nodes",
    "queue timeout",
    "queued request timed out",
    "session request timed out",
    "timed out waiting for a node",
    "timed out waiting for available device",
)
PRE_EXECUTION_ERROR_TYPES: tuple[str, ...] = (
    "sessionnotcreatedexception",
    "gridtimeoutexception",
    "sessionrequesttimeout",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _normalized_error_text(session: Session) -> str:
    return " ".join(part for part in (session.error_type, session.error_message) if part).lower()


def _matches_capacity_error(session: Session) -> bool:
    text_value = _normalized_error_text(session)
    return any(pattern in text_value for pattern in CAPACITY_ERROR_PATTERNS)


def _has_pre_execution_error_type(session: Session) -> bool:
    error_type = (session.error_type or "").lower()
    return any(pattern in error_type for pattern in PRE_EXECUTION_ERROR_TYPES)


def is_unmet_demand_session(session: Session) -> bool:
    if session.status not in {SessionStatus.failed, SessionStatus.error}:
        return False
    if not _matches_capacity_error(session):
        return False
    if session.device_id is None:
        return True
    if not _has_pre_execution_error_type(session):
        return False
    if session.ended_at is None:
        return False
    return session.ended_at - session.started_at <= timedelta(seconds=30)


def _bucket_start(timestamp: datetime, *, date_from: datetime, bucket_minutes: int) -> datetime:
    bucket_seconds = bucket_minutes * 60
    offset_seconds = max((timestamp - date_from).total_seconds(), 0)
    bucket_index = int(offset_seconds // bucket_seconds)
    return date_from + timedelta(seconds=bucket_index * bucket_seconds)


async def _rejected_unfulfilled_counts_by_bucket(
    db: AsyncSession,
    *,
    date_from: datetime,
    date_to: datetime,
    bucket_minutes: int,
) -> dict[datetime, int]:
    stmt = (
        select(Session)
        .where(
            Session.started_at >= date_from,
            Session.started_at < date_to,
            Session.status.in_((SessionStatus.failed, SessionStatus.error)),
            or_(Session.error_type.is_not(None), Session.error_message.is_not(None)),
        )
        .order_by(Session.started_at.asc())
    )
    stmt = exclude_non_test_sessions(stmt)
    result = await db.execute(stmt)

    counts: dict[datetime, int] = {}
    for session in result.scalars().all():
        if not is_unmet_demand_session(session):
            continue
        bucket = _bucket_start(session.started_at, date_from=date_from, bucket_minutes=bucket_minutes)
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


async def get_fleet_capacity_timeline(
    db: AsyncSession,
    *,
    date_from: datetime,
    date_to: datetime,
    bucket_minutes: int = DEFAULT_BUCKET_MINUTES,
) -> FleetCapacityTimeline:
    if date_from >= date_to:
        raise ValueError("date_from must be before date_to")
    if not 1 <= bucket_minutes <= MAX_BUCKET_MINUTES:
        raise ValueError("bucket_minutes must be between 1 and 1440")

    bucket_rows = (
        (
            await db.execute(
                text(
                    """
                SELECT
                    date_bin(
                        make_interval(mins => CAST(:bucket_minutes AS integer)),
                        captured_at,
                        :date_from
                    ) AS bucket_start,
                    MAX(total_capacity_slots)::int AS total_capacity_slots,
                    MAX(active_sessions)::int AS active_sessions,
                    MAX(queued_requests)::int AS queued_requests,
                    MAX(hosts_total)::int AS hosts_total,
                    MAX(hosts_online)::int AS hosts_online,
                    MAX(devices_total)::int AS devices_total,
                    MAX(devices_available)::int AS devices_available,
                    MAX(devices_offline)::int AS devices_offline,
                    MAX(devices_maintenance)::int AS devices_maintenance
                FROM analytics_capacity_snapshots
                WHERE captured_at >= :date_from
                  AND captured_at < :date_to
                GROUP BY bucket_start
                ORDER BY bucket_start ASC
                """
                ),
                {"bucket_minutes": bucket_minutes, "date_from": date_from, "date_to": date_to},
            )
        )
        .mappings()
        .all()
    )
    rejected_counts = await _rejected_unfulfilled_counts_by_bucket(
        db,
        date_from=date_from,
        date_to=date_to,
        bucket_minutes=bucket_minutes,
    )

    series: list[FleetCapacityTimelinePoint] = []
    for row in bucket_rows:
        timestamp = row["bucket_start"]
        total_capacity_slots = int(row["total_capacity_slots"] or 0)
        active_sessions = int(row["active_sessions"] or 0)
        queued_requests = int(row["queued_requests"] or 0)
        hosts_total = int(row["hosts_total"] or 0)
        hosts_online = int(row["hosts_online"] or 0)
        devices_total = int(row["devices_total"] or 0)
        devices_available = int(row["devices_available"] or 0)
        devices_offline = int(row["devices_offline"] or 0)
        devices_maintenance = int(row["devices_maintenance"] or 0)
        rejected_unfulfilled_sessions = rejected_counts.get(timestamp, 0)
        available_capacity_slots = max(total_capacity_slots - active_sessions, 0)
        series.append(
            FleetCapacityTimelinePoint(
                timestamp=timestamp,
                total_capacity_slots=total_capacity_slots,
                active_sessions=active_sessions,
                queued_requests=queued_requests,
                rejected_unfulfilled_sessions=rejected_unfulfilled_sessions,
                available_capacity_slots=available_capacity_slots,
                inferred_demand=active_sessions + queued_requests + rejected_unfulfilled_sessions,
                hosts_total=hosts_total,
                hosts_online=hosts_online,
                devices_total=devices_total,
                devices_available=devices_available,
                devices_offline=devices_offline,
                devices_maintenance=devices_maintenance,
            )
        )

    return FleetCapacityTimeline(
        date_from=date_from,
        date_to=date_to,
        bucket_minutes=bucket_minutes,
        series=series,
    )


def _extract_grid_counts(grid_data: dict[str, Any]) -> tuple[int, int] | None:
    value = grid_data.get("value")
    if not isinstance(value, dict):
        return None
    if grid_data.get("error") and not value.get("ready", False):
        return None

    nodes = value.get("nodes", [])
    active_sessions = 0
    if isinstance(nodes, list):
        active_sessions = sum(
            1
            for node in nodes
            if isinstance(node, dict)
            for slot in node.get("slots", [])
            if isinstance(slot, dict) and slot.get("session")
        )
    queue_requests = value.get("sessionQueueRequests", [])
    queued_requests = len(queue_requests) if isinstance(queue_requests, list) else 0
    return active_sessions, queued_requests


async def _count_schedulable_capacity(db: AsyncSession) -> int:
    stmt = (
        select(func.count())
        .select_from(Device)
        .join(AppiumNode, AppiumNode.device_id == Device.id)
        .where(
            Device.verified_at.is_not(None),
            Device.operational_state != DeviceOperationalState.offline,
            Device.hold.is_(None),
            AppiumNode.pid.is_not(None),
            AppiumNode.active_connection_target.is_not(None),
        )
    )
    return int((await db.execute(stmt)).scalar_one() or 0)


async def _count_hosts(db: AsyncSession) -> tuple[int, int]:
    stmt = select(
        func.count().label("total"),
        func.count().filter(Host.status == HostStatus.online).label("online"),
    ).select_from(Host)
    row = (await db.execute(stmt)).one()
    return int(row.total or 0), int(row.online or 0)


async def _count_devices(db: AsyncSession) -> tuple[int, int, int, int]:
    stmt = select(
        func.count().label("total"),
        func.count()
        .filter(and_(Device.operational_state == DeviceOperationalState.available, Device.hold.is_(None)))
        .label("available"),
        func.count()
        .filter(and_(Device.operational_state == DeviceOperationalState.offline, Device.hold.is_(None)))
        .label("offline"),
        func.count().filter(Device.hold == DeviceHold.maintenance).label("maintenance"),
    ).select_from(Device)
    row = (await db.execute(stmt)).one()
    return int(row.total or 0), int(row.available or 0), int(row.offline or 0), int(row.maintenance or 0)


async def collect_capacity_snapshot_once(
    db: AsyncSession,
    *,
    captured_at: datetime | None = None,
) -> AnalyticsCapacitySnapshot | None:
    grid_data = await grid_service.get_grid_status()
    grid_counts = _extract_grid_counts(grid_data)
    if grid_counts is None:
        logger.warning("Fleet capacity snapshot skipped because Grid status was unavailable")
        return None

    active_sessions, queued_requests = grid_counts
    total_capacity_slots = await _count_schedulable_capacity(db)
    hosts_total, hosts_online = await _count_hosts(db)
    devices_total, devices_available, devices_offline, devices_maintenance = await _count_devices(db)

    snapshot = AnalyticsCapacitySnapshot(
        captured_at=captured_at or _now(),
        total_capacity_slots=total_capacity_slots,
        active_sessions=active_sessions,
        queued_requests=queued_requests,
        available_capacity_slots=max(total_capacity_slots - active_sessions, 0),
        hosts_total=hosts_total,
        hosts_online=hosts_online,
        devices_total=devices_total,
        devices_available=devices_available,
        devices_offline=devices_offline,
        devices_maintenance=devices_maintenance,
    )
    db.add(snapshot)
    await db.commit()
    await db.refresh(snapshot)
    return snapshot


async def fleet_capacity_collector_loop() -> None:
    while True:
        interval = float(settings_service.get("general.fleet_capacity_snapshot_interval_sec"))
        try:
            async with observe_background_loop(LOOP_NAME, interval).cycle(), async_session() as db:
                await collect_capacity_snapshot_once(db)
        except Exception:
            logger.exception("Fleet capacity collector failed")
        await asyncio.sleep(interval)
