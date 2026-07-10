from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, func, or_, select, text

from app.analytics import schemas as analytics_schemas
from app.analytics.models import AnalyticsCapacitySnapshot
from app.appium_nodes.models import AppiumNode
from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.devices.models import Device
from app.devices.services.claims import active_reservation_exists
from app.devices.services.state import (
    is_available_sql,
    is_maintenance_sql,
    is_offline_sql,
)
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.hosts.liveness import host_online_clause
from app.hosts.models import Host
from app.sessions.filters import exclude_non_test_sessions
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session, SessionStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)
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


def _align_window_to_buckets(
    *,
    date_from: datetime,
    date_to: datetime,
    bucket_minutes: int,
) -> tuple[datetime, datetime]:
    # Callers must validate date_from < date_to before invoking. The max(..., 1)
    # below is a defensive floor: if a degenerate equal-bounds input ever slips
    # past validation, the helper still emits a single bucket instead of an
    # empty series that would silently break generate_series consumers.
    bucket = timedelta(minutes=bucket_minutes)
    bucket_seconds = bucket.total_seconds()
    aligned_from_index = math.floor(date_from.timestamp() / bucket_seconds)
    aligned_from = datetime.fromtimestamp(aligned_from_index * bucket_seconds, tz=UTC)
    total = (date_to - aligned_from).total_seconds() / bucket_seconds
    n_buckets = max(math.ceil(total), 1)
    aligned_to = aligned_from + bucket * n_buckets
    return aligned_from, aligned_to


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


async def _count_active_sessions(db: AsyncSession) -> int:
    # Count ``pending`` too: state derivation derives a pending-claimed device
    # ``busy`` and counts it in total_capacity_slots, so omitting pending here
    # over-reports available headroom during create bursts (C13). Shared via
    # live_session_predicate.
    stmt = select(func.count()).select_from(Session).where(live_session_predicate())
    return int((await db.execute(stmt)).scalar_one() or 0)


async def _count_queued_requests(db: AsyncSession) -> int:
    stmt = (
        select(func.count())
        .select_from(GridSessionQueueTicket)
        .where(GridSessionQueueTicket.status == GridQueueStatus.waiting)
    )
    return int((await db.execute(stmt)).scalar_one() or 0)


async def _count_schedulable_capacity(db: AsyncSession) -> int:
    now = now_utc()
    stmt = (
        select(func.count())
        .select_from(Device)
        .join(AppiumNode, AppiumNode.device_id == Device.id)
        .where(
            Device.verified_at.is_not(None),
            ~is_offline_sql(now=now),
            ~active_reservation_exists(),
            AppiumNode.pid.is_not(None),
            AppiumNode.active_connection_target.is_not(None),
        )
    )
    return int((await db.execute(stmt)).scalar_one() or 0)


async def _count_hosts(db: AsyncSession, *, offline_after_sec: float) -> tuple[int, int]:
    stmt = select(
        func.count().label("total"),
        func.count().filter(host_online_clause(offline_after_sec=offline_after_sec)).label("online"),
    ).select_from(Host)
    row = (await db.execute(stmt)).one()
    return int(row.total or 0), int(row.online or 0)


async def _count_devices(db: AsyncSession) -> tuple[int, int, int, int]:
    now = now_utc()
    stmt = select(
        func.count().label("total"),
        func.count().filter(and_(is_available_sql(now=now), ~active_reservation_exists())).label("available"),
        func.count().filter(and_(is_offline_sql(now=now), ~active_reservation_exists())).label("offline"),
        func.count().filter(is_maintenance_sql(now=now)).label("maintenance"),
    ).select_from(Device)
    row = (await db.execute(stmt)).one()
    return int(row.total or 0), int(row.available or 0), int(row.offline or 0), int(row.maintenance or 0)


class FleetCapacityService:
    async def get_fleet_capacity_timeline(
        self,
        db: AsyncSession,
        *,
        date_from: datetime,
        date_to: datetime,
        bucket_minutes: int = DEFAULT_BUCKET_MINUTES,
    ) -> analytics_schemas.FleetCapacityTimeline:
        if date_from >= date_to:
            raise ValueError("date_from must be before date_to")
        if not 1 <= bucket_minutes <= MAX_BUCKET_MINUTES:
            raise ValueError("bucket_minutes must be between 1 and 1440")

        aligned_from, aligned_to = _align_window_to_buckets(
            date_from=date_from,
            date_to=date_to,
            bucket_minutes=bucket_minutes,
        )

        bucket_rows = (
            (
                await db.execute(
                    text(
                        """
                        WITH bucket_axis AS (
                            SELECT generate_series(
                                CAST(:date_from AS timestamptz),
                                CAST(:date_to AS timestamptz) - make_interval(mins => CAST(:bucket_minutes AS integer)),
                                make_interval(mins => CAST(:bucket_minutes AS integer))
                            ) AS bucket_start
                        ),
                        binned AS (
                            SELECT DISTINCT ON (bucket_start)
                                date_bin(
                                    make_interval(mins => CAST(:bucket_minutes AS integer)),
                                    captured_at,
                                    CAST(:date_from AS timestamptz)
                                ) AS bucket_start,
                                total_capacity_slots,
                                active_sessions,
                                queued_requests,
                                hosts_total,
                                hosts_online,
                                devices_total,
                                devices_available,
                                devices_offline,
                                devices_maintenance
                            FROM analytics_capacity_snapshots
                            WHERE captured_at >= CAST(:date_from AS timestamptz)
                              AND captured_at < CAST(:date_to AS timestamptz)
                            ORDER BY bucket_start ASC, captured_at DESC
                        )
                        SELECT
                            bucket_axis.bucket_start AS bucket_start,
                            (binned.bucket_start IS NOT NULL) AS has_data,
                            COALESCE(binned.total_capacity_slots, 0)::int AS total_capacity_slots,
                            COALESCE(binned.active_sessions, 0)::int AS active_sessions,
                            COALESCE(binned.queued_requests, 0)::int AS queued_requests,
                            COALESCE(binned.hosts_total, 0)::int AS hosts_total,
                            COALESCE(binned.hosts_online, 0)::int AS hosts_online,
                            COALESCE(binned.devices_total, 0)::int AS devices_total,
                            COALESCE(binned.devices_available, 0)::int AS devices_available,
                            COALESCE(binned.devices_offline, 0)::int AS devices_offline,
                            COALESCE(binned.devices_maintenance, 0)::int AS devices_maintenance
                        FROM bucket_axis
                        LEFT JOIN binned USING (bucket_start)
                        ORDER BY bucket_axis.bucket_start ASC
                        """
                    ),
                    {
                        "bucket_minutes": bucket_minutes,
                        "date_from": aligned_from,
                        "date_to": aligned_to,
                    },
                )
            )
            .mappings()
            .all()
        )
        rejected_counts = await _rejected_unfulfilled_counts_by_bucket(
            db,
            date_from=aligned_from,
            date_to=aligned_to,
            bucket_minutes=bucket_minutes,
        )

        series: list[analytics_schemas.FleetCapacityTimelinePoint] = []
        for row in bucket_rows:
            timestamp = row["bucket_start"]
            has_data = bool(row["has_data"])
            total_capacity_slots = int(row["total_capacity_slots"] or 0)
            active_sessions = int(row["active_sessions"] or 0)
            queued_requests = int(row["queued_requests"] or 0)
            hosts_total = int(row["hosts_total"] or 0)
            hosts_online = int(row["hosts_online"] or 0)
            devices_total = int(row["devices_total"] or 0)
            devices_available = int(row["devices_available"] or 0)
            devices_offline = int(row["devices_offline"] or 0)
            devices_maintenance = int(row["devices_maintenance"] or 0)
            rejected_unfulfilled_sessions = rejected_counts.get(timestamp, 0) if has_data else 0
            available_capacity_slots = max(total_capacity_slots - active_sessions, 0)
            inferred_demand = active_sessions + queued_requests + rejected_unfulfilled_sessions if has_data else 0
            series.append(
                analytics_schemas.FleetCapacityTimelinePoint(
                    timestamp=timestamp,
                    total_capacity_slots=total_capacity_slots,
                    active_sessions=active_sessions,
                    queued_requests=queued_requests,
                    rejected_unfulfilled_sessions=rejected_unfulfilled_sessions,
                    available_capacity_slots=available_capacity_slots,
                    inferred_demand=inferred_demand,
                    hosts_total=hosts_total,
                    hosts_online=hosts_online,
                    devices_total=devices_total,
                    devices_available=devices_available,
                    devices_offline=devices_offline,
                    devices_maintenance=devices_maintenance,
                    has_data=has_data,
                )
            )

        return analytics_schemas.FleetCapacityTimeline(
            date_from=aligned_from,
            date_to=aligned_to,
            bucket_minutes=bucket_minutes,
            series=series,
        )

    async def collect_capacity_snapshot_once(
        self,
        db: AsyncSession,
        *,
        offline_after_sec: float,
        captured_at: datetime | None = None,
    ) -> AnalyticsCapacitySnapshot | None:
        active_sessions = await _count_active_sessions(db)
        queued_requests = await _count_queued_requests(db)
        total_capacity_slots = await _count_schedulable_capacity(db)
        hosts_total, hosts_online = await _count_hosts(db, offline_after_sec=offline_after_sec)
        devices_total, devices_available, devices_offline, devices_maintenance = await _count_devices(db)

        snapshot = AnalyticsCapacitySnapshot(
            captured_at=captured_at or now_utc(),
            total_capacity_slots=total_capacity_slots,
            active_sessions=active_sessions,
            queued_requests=queued_requests,
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
