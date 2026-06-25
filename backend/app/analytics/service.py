from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.analytics.schemas import (
    DeviceReliabilityRow,
    DeviceUtilizationRow,
    FleetOverview,
    GroupByOption,
    SessionSummaryRow,
)
from app.devices.models import Device, DeviceEvent, DeviceEventType
from app.sessions.filters import exclude_non_success_metric_sessions, exclude_non_test_sessions
from app.sessions.models import Session, SessionStatus

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


async def get_session_summary(
    db: AsyncSession,
    date_from: datetime,
    date_to: datetime,
    group_by: GroupByOption,
) -> list[SessionSummaryRow]:
    group_col: Any
    if group_by == GroupByOption.platform:
        group_col = Device.platform_id
    elif group_by == GroupByOption.os_version:
        group_col = Device.os_version
    elif group_by == GroupByOption.device_id:
        group_col = Session.device_id
    else:  # day
        group_col = func.date_trunc("day", Session.started_at)

    duration_expr = func.extract("epoch", Session.ended_at - Session.started_at)

    stmt = (
        select(
            group_col.label("group_key"),
            func.count().label("total"),
            func.count().filter(Session.status == SessionStatus.passed).label("passed"),
            func.count().filter(Session.status == SessionStatus.failed).label("failed"),
            func.count().filter(Session.status == SessionStatus.error).label("error"),
            func.avg(duration_expr).filter(Session.ended_at.isnot(None)).label("avg_duration_sec"),
        )
        .join(Device, Session.device_id == Device.id)
        .where(Session.started_at >= date_from, Session.started_at < date_to)
        .group_by(group_col)
        .order_by(group_col)
    )
    stmt = exclude_non_success_metric_sessions(stmt)

    result = await db.execute(stmt)
    rows = result.all()

    return [
        SessionSummaryRow(
            group_key=str(row.group_key),
            total=row.total,
            passed=row.passed,
            failed=row.failed,
            error=row.error,
            avg_duration_sec=round(row.avg_duration_sec, 2) if row.avg_duration_sec else None,
        )
        for row in rows
    ]


async def get_device_utilization(
    db: AsyncSession,
    date_from: datetime,
    date_to: datetime,
) -> list[DeviceUtilizationRow]:
    total_range_sec = (date_to - date_from).total_seconds()
    if total_range_sec <= 0:
        return []

    overlap_start = func.greatest(Session.started_at, date_from)
    overlap_end = func.least(func.coalesce(Session.ended_at, date_to), date_to)
    session_time_expr = func.extract("epoch", overlap_end - overlap_start)

    stmt = (
        select(
            Session.device_id,
            Device.name.label("device_name"),
            Device.platform_id,
            func.sum(session_time_expr).label("total_session_time_sec"),
            func.count(Session.id).label("session_count"),
        )
        .join(Device, Session.device_id == Device.id)
        .where(Session.started_at < date_to)
        .where(func.coalesce(Session.ended_at, date_to) > date_from)
        .group_by(Session.device_id, Device.name, Device.platform_id)
        .order_by(func.sum(session_time_expr).desc())
    )
    stmt = exclude_non_test_sessions(stmt)

    result = await db.execute(stmt)
    rows = result.all()

    return [
        DeviceUtilizationRow(
            device_id=str(row.device_id),
            device_name=row.device_name,
            platform_id=row.platform_id,
            total_session_time_sec=round(float(row.total_session_time_sec or 0), 2),
            idle_time_sec=round(max(total_range_sec - float(row.total_session_time_sec or 0), 0), 2),
            busy_pct=round(min(float(row.total_session_time_sec or 0) / total_range_sec * 100, 100), 2),
            session_count=row.session_count,
        )
        for row in rows
    ]


async def get_device_reliability(
    db: AsyncSession,
    date_from: datetime,
    date_to: datetime,
) -> list[DeviceReliabilityRow]:
    stmt = (
        select(
            DeviceEvent.device_id,
            Device.name.label("device_name"),
            Device.platform_id,
            func.count()
            .filter(DeviceEvent.event_type == DeviceEventType.health_check_fail)
            .label("health_check_failures"),
            func.count()
            .filter(DeviceEvent.event_type == DeviceEventType.connectivity_lost)
            .label("connectivity_losses"),
            func.count().filter(DeviceEvent.event_type == DeviceEventType.node_crash).label("node_crashes"),
            func.count().label("total_incidents"),
        )
        .join(Device, DeviceEvent.device_id == Device.id)
        .where(
            DeviceEvent.created_at >= date_from,
            DeviceEvent.created_at < date_to,
            DeviceEvent.event_type.in_(
                [
                    DeviceEventType.health_check_fail,
                    DeviceEventType.connectivity_lost,
                    DeviceEventType.node_crash,
                ]
            ),
        )
        .group_by(DeviceEvent.device_id, Device.name, Device.platform_id)
        .order_by(func.count().desc())
    )

    result = await db.execute(stmt)
    rows = result.all()

    return [
        DeviceReliabilityRow(
            device_id=str(row.device_id),
            device_name=row.device_name,
            platform_id=row.platform_id,
            health_check_failures=row.health_check_failures,
            connectivity_losses=row.connectivity_losses,
            node_crashes=row.node_crashes,
            total_incidents=row.total_incidents,
        )
        for row in rows
    ]


async def get_fleet_overview(
    db: AsyncSession,
    date_from: datetime,
    date_to: datetime,
) -> FleetOverview:
    utilization = await get_device_utilization(db, date_from, date_to)
    avg_utilization = round(sum(d.busy_pct for d in utilization) / len(utilization), 2) if utilization else 0

    # Pass rate
    pass_stmt = select(
        func.count().label("total"),
        func.count().filter(Session.status == SessionStatus.passed).label("passed"),
    ).where(Session.started_at >= date_from, Session.started_at < date_to)
    pass_stmt = exclude_non_success_metric_sessions(pass_stmt)
    pass_result = await db.execute(pass_stmt)
    pass_row = pass_result.one()
    pass_rate = round(pass_row.passed / pass_row.total * 100, 2) if pass_row.total > 0 else None

    return FleetOverview(avg_utilization_pct=avg_utilization, pass_rate_pct=pass_rate)
