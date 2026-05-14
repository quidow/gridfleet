from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.schemas import (
    DeviceReliabilityRow,
    DeviceUtilizationRow,
    FleetDeviceSummary,
    FleetOverview,
    GroupByOption,
    SessionSummaryRow,
)
from app.models.device import Device
from app.models.device_event import DeviceEvent, DeviceEventType
from app.models.session import Session, SessionStatus
from app.services.session_filters import exclude_non_success_metric_sessions, exclude_non_test_sessions


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
    # Devices by platform_id
    platform_stmt = select(Device.platform_id, func.count()).group_by(Device.platform_id)
    platform_result = await db.execute(platform_stmt)
    devices_by_platform = {str(row[0]): row[1] for row in platform_result.all()}

    # Utilization data
    utilization = await get_device_utilization(db, date_from, date_to)
    avg_utilization = round(sum(d.busy_pct for d in utilization) / len(utilization), 2) if utilization else 0

    sorted_by_usage = sorted(utilization, key=lambda d: d.busy_pct, reverse=True)
    most_used = [
        FleetDeviceSummary(
            device_id=d.device_id, device_name=d.device_name, platform_id=d.platform_id, value=d.busy_pct
        )
        for d in sorted_by_usage[:5]
    ]
    least_used = [
        FleetDeviceSummary(
            device_id=d.device_id, device_name=d.device_name, platform_id=d.platform_id, value=d.busy_pct
        )
        for d in sorted_by_usage[-5:][::-1]
        if sorted_by_usage
    ]

    # Reliability data
    reliability = await get_device_reliability(db, date_from, date_to)
    sorted_by_reliability = sorted(reliability, key=lambda d: d.total_incidents)
    most_reliable = [
        FleetDeviceSummary(
            device_id=d.device_id,
            device_name=d.device_name,
            platform_id=d.platform_id,
            value=float(d.total_incidents),
        )
        for d in sorted_by_reliability[:5]
    ]
    least_reliable = [
        FleetDeviceSummary(
            device_id=d.device_id,
            device_name=d.device_name,
            platform_id=d.platform_id,
            value=float(d.total_incidents),
        )
        for d in sorted_by_reliability[-5:][::-1]
        if sorted_by_reliability
    ]

    # Pass rate
    pass_stmt = select(
        func.count().label("total"),
        func.count().filter(Session.status == SessionStatus.passed).label("passed"),
    ).where(Session.started_at >= date_from, Session.started_at < date_to)
    pass_stmt = exclude_non_success_metric_sessions(pass_stmt)
    pass_result = await db.execute(pass_stmt)
    pass_row = pass_result.one()
    pass_rate = round(pass_row.passed / pass_row.total * 100, 2) if pass_row.total > 0 else None

    # Devices needing attention (>5 incidents in range)
    attention_stmt = (
        select(func.count(func.distinct(DeviceEvent.device_id)))
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
        .group_by(DeviceEvent.device_id)
        .having(func.count() > 5)
    )
    # Wrap to count the number of devices matching the having clause
    attention_result = await db.execute(select(func.count()).select_from(attention_stmt.subquery()))
    devices_needing_attention = attention_result.scalar() or 0

    return FleetOverview(
        devices_by_platform=devices_by_platform,
        avg_utilization_pct=avg_utilization,
        most_used=most_used,
        least_used=least_used,
        most_reliable=most_reliable,
        least_reliable=least_reliable,
        pass_rate_pct=pass_rate,
        devices_needing_attention=devices_needing_attention,
    )
