"""Background task that deletes old data based on retention settings."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, delete, or_, select

from app.analytics.models import AnalyticsCapacitySnapshot
from app.core.background_loop import BackgroundLoop
from app.core.observability import get_logger, schedule_background_loop
from app.core.timeutil import now_utc
from app.devices.models import DeviceEvent, DeviceTestDataAuditLog
from app.events.models import SystemEvent
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.hosts.models import HostResourceSample
from app.jobs.models import Job
from app.jobs.statuses import JOB_STATUS_COMPLETED, JOB_STATUS_FAILED
from app.runs.models import TERMINAL_STATES, TestRun
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME
from app.settings.models import ConfigAuditLog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import InstrumentedAttribute
    from sqlalchemy.sql.elements import ColumnElement

    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory
    from app.devices.services_container import DeviceServices
    from app.events.protocols import EventPublisher

logger = get_logger(__name__)
LOOP_NAME = "data_cleanup"
DELETE_BATCH_SIZE = 1000
MAX_BATCHES_PER_TABLE = 10
CleanupModel = (
    type[Session]
    | type[ConfigAuditLog]
    | type[DeviceTestDataAuditLog]
    | type[DeviceEvent]
    | type[HostResourceSample]
    | type[AnalyticsCapacitySnapshot]
    | type[GridSessionQueueTicket]
    | type[SystemEvent]
    | type[TestRun]
    | type[Job]
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


@dataclass
class _CleanupCounts:
    sessions_deleted: int = 0
    probe_sessions_deleted: int = 0
    grid_queue_tickets_deleted: int = 0
    audit_deleted: int = 0
    test_data_audit_deleted: int = 0
    events_deleted: int = 0
    host_resource_samples_deleted: int = 0
    capacity_snapshots_deleted: int = 0
    system_events_deleted: int = 0
    test_runs_deleted: int = 0
    jobs_deleted: int = 0


class DataCleanupService:
    def __init__(self, *, publisher: EventPublisher, settings: SettingsReader) -> None:
        self._publisher = publisher
        self._settings = settings

    async def _cleanup_sessions_and_tickets(self, db: AsyncSession, now: datetime, counts: _CleanupCounts) -> None:
        # Sessions (only completed ones) — exclude probe rows; they have their own
        # retention.probe_sessions_days window below.
        sessions_days: int = self._settings.get("retention.sessions_days")
        if sessions_days > 0:
            cutoff = now - timedelta(days=sessions_days)
            counts.sessions_deleted = await _delete_in_batches(
                db,
                model=Session,
                timestamp_column=Session.started_at,
                cutoff=cutoff,
                extra_predicates=(
                    Session.status != SessionStatus.running,
                    Session.ended_at.is_not(None),
                    or_(Session.test_name.is_(None), Session.test_name != PROBE_TEST_NAME),
                ),
            )

        probe_sessions_days: int = self._settings.get("retention.probe_sessions_days")
        if probe_sessions_days > 0:
            cutoff = now - timedelta(days=probe_sessions_days)
            counts.probe_sessions_deleted = await _delete_in_batches(
                db,
                model=Session,
                timestamp_column=Session.started_at,
                cutoff=cutoff,
                extra_predicates=(Session.test_name == PROBE_TEST_NAME,),
            )

        # Terminal grid_session_queue tickets (reuses retention.sessions_days — a ticket
        # never outlives its allocation Session). Purges cancelled/expired plus dangling
        # `claimed` rows whose Session was already deleted (FK SET NULL) — legacy junk
        # the harness G7 invariant flags. `updated_at` is when it reached its terminal.
        if sessions_days > 0:
            cutoff = now - timedelta(days=sessions_days)
            counts.grid_queue_tickets_deleted = await _delete_in_batches(
                db,
                model=GridSessionQueueTicket,
                timestamp_column=GridSessionQueueTicket.updated_at,
                cutoff=cutoff,
                extra_predicates=(
                    or_(
                        GridSessionQueueTicket.status.in_((GridQueueStatus.cancelled, GridQueueStatus.expired)),
                        and_(
                            GridSessionQueueTicket.status == GridQueueStatus.claimed,
                            GridSessionQueueTicket.session_row_id.is_(None),
                        ),
                    ),
                ),
            )

    async def _cleanup_audit(self, db: AsyncSession, now: datetime, counts: _CleanupCounts) -> None:
        # ConfigAuditLog
        audit_days: int = self._settings.get("retention.audit_log_days")
        if audit_days > 0:
            cutoff = now - timedelta(days=audit_days)
            counts.audit_deleted = await _delete_in_batches(
                db,
                model=ConfigAuditLog,
                timestamp_column=ConfigAuditLog.changed_at,
                cutoff=cutoff,
            )

        # DeviceTestDataAuditLog (reuses retention.audit_log_days)
        if audit_days > 0:
            cutoff = now - timedelta(days=audit_days)
            counts.test_data_audit_deleted = await _delete_in_batches(
                db,
                model=DeviceTestDataAuditLog,
                timestamp_column=DeviceTestDataAuditLog.changed_at,
                cutoff=cutoff,
            )
        else:
            counts.test_data_audit_deleted = 0

    async def _cleanup_events_and_telemetry(self, db: AsyncSession, now: datetime, counts: _CleanupCounts) -> None:
        # DeviceEvent
        events_days: int = self._settings.get("retention.device_events_days")
        if events_days > 0:
            cutoff = now - timedelta(days=events_days)
            counts.events_deleted = await _delete_in_batches(
                db,
                model=DeviceEvent,
                timestamp_column=DeviceEvent.created_at,
                cutoff=cutoff,
            )

        # HostResourceSample
        host_resource_telemetry_hours: int = self._settings.get("retention.host_resource_telemetry_hours")
        if host_resource_telemetry_hours > 0:
            cutoff = now - timedelta(hours=host_resource_telemetry_hours)
            counts.host_resource_samples_deleted = await _delete_in_batches(
                db,
                model=HostResourceSample,
                timestamp_column=HostResourceSample.recorded_at,
                cutoff=cutoff,
            )

        capacity_snapshots_days: int = self._settings.get("retention.capacity_snapshots_days")
        if capacity_snapshots_days > 0:
            cutoff = now - timedelta(days=capacity_snapshots_days)
            counts.capacity_snapshots_deleted = await _delete_in_batches(
                db,
                model=AnalyticsCapacitySnapshot,
                timestamp_column=AnalyticsCapacitySnapshot.captured_at,
                cutoff=cutoff,
            )

        system_events_days: int = self._settings.get("retention.system_events_days")
        if system_events_days > 0:
            cutoff = now - timedelta(days=system_events_days)
            counts.system_events_deleted = await _delete_in_batches(
                db,
                model=SystemEvent,
                timestamp_column=SystemEvent.created_at,
                cutoff=cutoff,
            )

    async def _cleanup_runs_and_jobs(self, db: AsyncSession, now: datetime, counts: _CleanupCounts) -> None:
        # TestRun (terminal states only) — device_reservations cascade via FK ON DELETE CASCADE.
        # Cutoff on created_at: terminal runs cannot resurrect, and created_at is never NULL
        # (completed_at is NULL for reaper-expired runs).
        test_runs_days: int = self._settings.get("retention.test_runs_days")
        if test_runs_days > 0:
            cutoff = now - timedelta(days=test_runs_days)
            counts.test_runs_deleted = await _delete_in_batches(
                db,
                model=TestRun,
                timestamp_column=TestRun.created_at,
                cutoff=cutoff,
                extra_predicates=(TestRun.state.in_(TERMINAL_STATES),),
            )

        # Job (terminal statuses only) — pending/running rows are the worker's queue, never touched.
        jobs_days: int = self._settings.get("retention.jobs_days")
        if jobs_days > 0:
            cutoff = now - timedelta(days=jobs_days)
            counts.jobs_deleted = await _delete_in_batches(
                db,
                model=Job,
                timestamp_column=Job.created_at,
                cutoff=cutoff,
                extra_predicates=(Job.status.in_((JOB_STATUS_COMPLETED, JOB_STATUS_FAILED)),),
            )

    async def cleanup_old_data(self, db: AsyncSession) -> None:
        now = now_utc()
        started = time.monotonic()
        counts = _CleanupCounts()

        await self._cleanup_sessions_and_tickets(db, now, counts)
        await self._cleanup_audit(db, now, counts)
        await self._cleanup_events_and_telemetry(db, now, counts)
        await self._cleanup_runs_and_jobs(db, now, counts)

        logger.info(
            "Data cleanup completed: sessions=%d, probe_sessions=%d, audit_logs=%d, test_data_audit_logs=%d, "
            "device_events=%d, host_resource_samples=%d, capacity_snapshots=%d, "
            "grid_queue_tickets=%d"
            ", system_events=%d, test_runs=%d, jobs=%d",
            counts.sessions_deleted,
            counts.probe_sessions_deleted,
            counts.audit_deleted,
            counts.test_data_audit_deleted,
            counts.events_deleted,
            counts.host_resource_samples_deleted,
            counts.capacity_snapshots_deleted,
            counts.grid_queue_tickets_deleted,
            counts.system_events_deleted,
            counts.test_runs_deleted,
            counts.jobs_deleted,
        )
        await self._publisher.publish(
            "system.cleanup_completed",
            {
                "sessions_deleted": counts.sessions_deleted,
                "probe_sessions_deleted": counts.probe_sessions_deleted,
                "audit_entries_deleted": counts.audit_deleted,
                "test_data_audit_entries_deleted": counts.test_data_audit_deleted,
                "device_events_deleted": counts.events_deleted,
                "host_resource_samples_deleted": counts.host_resource_samples_deleted,
                "capacity_snapshots_deleted": counts.capacity_snapshots_deleted,
                "grid_queue_tickets_deleted": counts.grid_queue_tickets_deleted,
                "system_events_deleted": counts.system_events_deleted,
                "test_runs_deleted": counts.test_runs_deleted,
                "jobs_deleted": counts.jobs_deleted,
                "duration_seconds": round(time.monotonic() - started, 3),
            },
        )


class DataCleanupLoop(BackgroundLoop):
    """Background loop that periodically cleans up old data."""

    loop_name = LOOP_NAME
    cycle_failed_message = "Data cleanup failed"
    sleep_before_first_cycle = True  # never run cleanup immediately at boot

    def __init__(self, *, services: DeviceServices) -> None:
        self._services = services
        self._interval_sec = 0.0

    @property
    def _session_factory(self) -> SessionFactory:
        return self._services.session_factory

    async def _on_start(self) -> None:
        interval_hours: int = self._services.settings.get("retention.cleanup_interval_hours")
        self._interval_sec = float(interval_hours * 3600)
        # Pre-register the snapshot so readiness probes see the loop before its first (long) sleep.
        await schedule_background_loop(LOOP_NAME, self._interval_sec)

    def _interval(self) -> float:
        return self._interval_sec

    async def _run_cycle(self, db: AsyncSession) -> None:
        await self._services.data_cleanup.cleanup_old_data(db)
