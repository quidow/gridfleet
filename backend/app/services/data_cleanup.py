"""Background task that deletes old data based on retention settings."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from app.database import async_session
from app.models.analytics_capacity_snapshot import AnalyticsCapacitySnapshot
from app.models.config_audit_log import ConfigAuditLog
from app.models.device_event import DeviceEvent
from app.models.host_resource_sample import HostResourceSample
from app.models.session import Session, SessionStatus
from app.observability import get_logger, observe_background_loop, schedule_background_loop
from app.services.event_bus import event_bus
from app.services.settings_service import settings_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import InstrumentedAttribute
    from sqlalchemy.sql.elements import ColumnElement

logger = get_logger(__name__)
LOOP_NAME = "data_cleanup"
DELETE_BATCH_SIZE = 1000
MAX_BATCHES_PER_TABLE = 10
CleanupModel = (
    type[Session]
    | type[ConfigAuditLog]
    | type[DeviceEvent]
    | type[HostResourceSample]
    | type[AnalyticsCapacitySnapshot]
)


async def _delete_in_batches(
    db: AsyncSession,
    *,
    model: CleanupModel,
    timestamp_column: InstrumentedAttribute[datetime],
    cutoff: datetime,
    extra_predicates: tuple[ColumnElement[bool], ...] = (),
) -> int:
    deleted_total = 0
    id_column = model.id
    order_columns = (timestamp_column.asc(), id_column.asc())

    for _ in range(MAX_BATCHES_PER_TABLE):
        id_subquery = (
            select(id_column)
            .where(timestamp_column < cutoff, *extra_predicates)
            .order_by(*order_columns)
            .limit(DELETE_BATCH_SIZE)
        )
        result = await db.execute(delete(model).where(id_column.in_(id_subquery)))
        deleted = int(getattr(result, "rowcount", 0) or 0)
        if deleted == 0:
            break
        deleted_total += deleted
        await db.commit()

    return deleted_total


async def _cleanup_old_data(db: AsyncSession) -> None:
    now = datetime.now(UTC)
    sessions_deleted = 0
    audit_deleted = 0
    events_deleted = 0
    host_resource_samples_deleted = 0
    capacity_snapshots_deleted = 0

    # Sessions (only completed ones)
    sessions_days: int = settings_service.get("retention.sessions_days")
    if sessions_days > 0:
        cutoff = now - timedelta(days=sessions_days)
        sessions_deleted = await _delete_in_batches(
            db,
            model=Session,
            timestamp_column=Session.started_at,
            cutoff=cutoff,
            extra_predicates=(
                Session.status != SessionStatus.running,
                Session.ended_at.is_not(None),
            ),
        )

    # ConfigAuditLog
    audit_days: int = settings_service.get("retention.audit_log_days")
    if audit_days > 0:
        cutoff = now - timedelta(days=audit_days)
        audit_deleted = await _delete_in_batches(
            db,
            model=ConfigAuditLog,
            timestamp_column=ConfigAuditLog.changed_at,
            cutoff=cutoff,
        )

    # DeviceEvent
    events_days: int = settings_service.get("retention.device_events_days")
    if events_days > 0:
        cutoff = now - timedelta(days=events_days)
        events_deleted = await _delete_in_batches(
            db,
            model=DeviceEvent,
            timestamp_column=DeviceEvent.created_at,
            cutoff=cutoff,
        )

    # HostResourceSample
    host_resource_telemetry_hours: int = settings_service.get("retention.host_resource_telemetry_hours")
    if host_resource_telemetry_hours > 0:
        cutoff = now - timedelta(hours=host_resource_telemetry_hours)
        host_resource_samples_deleted = await _delete_in_batches(
            db,
            model=HostResourceSample,
            timestamp_column=HostResourceSample.recorded_at,
            cutoff=cutoff,
        )

    capacity_snapshots_days: int = settings_service.get("retention.capacity_snapshots_days")
    if capacity_snapshots_days > 0:
        cutoff = now - timedelta(days=capacity_snapshots_days)
        capacity_snapshots_deleted = await _delete_in_batches(
            db,
            model=AnalyticsCapacitySnapshot,
            timestamp_column=AnalyticsCapacitySnapshot.captured_at,
            cutoff=cutoff,
        )

    logger.info(
        "Data cleanup completed: sessions=%d, audit_logs=%d, device_events=%d, "
        "host_resource_samples=%d, capacity_snapshots=%d",
        sessions_deleted,
        audit_deleted,
        events_deleted,
        host_resource_samples_deleted,
        capacity_snapshots_deleted,
    )
    await event_bus.publish(
        "system.cleanup_completed",
        {
            "sessions_deleted": sessions_deleted,
            "audit_entries_deleted": audit_deleted,
            "device_events_deleted": events_deleted,
            "host_resource_samples_deleted": host_resource_samples_deleted,
            "capacity_snapshots_deleted": capacity_snapshots_deleted,
        },
    )


async def data_cleanup_loop() -> None:
    """Background loop that periodically cleans up old data."""
    interval_hours: int = settings_service.get("retention.cleanup_interval_hours")
    interval_seconds = float(interval_hours * 3600)
    await schedule_background_loop(LOOP_NAME, interval_seconds)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            async with observe_background_loop(LOOP_NAME, interval_seconds).cycle(), async_session() as db:
                await _cleanup_old_data(db)
        except Exception:
            logger.exception("Data cleanup failed")
