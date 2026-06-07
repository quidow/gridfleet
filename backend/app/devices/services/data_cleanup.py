"""Background task that deletes old data based on retention settings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, delete, or_, select

from app.agent_comm.models import AgentReconfigureOutbox
from app.analytics.models import AnalyticsCapacitySnapshot
from app.core.background_loop import BackgroundLoop
from app.core.observability import get_logger, schedule_background_loop
from app.devices.models import DeviceEvent, DeviceTestDataAuditLog
from app.diagnostics.models import DeviceDiagnosticSnapshot
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.hosts.models import HostAgentLogEntry, HostResourceSample
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
    | type[AgentReconfigureOutbox]
    | type[ConfigAuditLog]
    | type[DeviceTestDataAuditLog]
    | type[DeviceEvent]
    | type[DeviceDiagnosticSnapshot]
    | type[HostResourceSample]
    | type[HostAgentLogEntry]
    | type[AnalyticsCapacitySnapshot]
    | type[GridSessionQueueTicket]
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


class DataCleanupService:
    def __init__(self, *, publisher: EventPublisher, settings: SettingsReader) -> None:
        self._publisher = publisher
        self._settings = settings

    async def cleanup_old_data(self, db: AsyncSession) -> None:
        now = datetime.now(UTC)
        sessions_deleted = 0
        audit_deleted = 0
        test_data_audit_deleted = 0
        events_deleted = 0
        host_resource_samples_deleted = 0
        agent_log_entries_deleted = 0
        capacity_snapshots_deleted = 0
        diagnostic_snapshots_deleted = 0
        agent_reconfigure_outbox_deleted = 0
        grid_queue_tickets_deleted = 0

        # Sessions (only completed ones) — exclude probe rows; they have their own
        # retention.probe_sessions_days window below.
        sessions_days: int = self._settings.get("retention.sessions_days")
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
                    or_(Session.test_name.is_(None), Session.test_name != PROBE_TEST_NAME),
                ),
            )

        probe_sessions_days: int = self._settings.get("retention.probe_sessions_days")
        probe_sessions_deleted = 0
        if probe_sessions_days > 0:
            cutoff = now - timedelta(days=probe_sessions_days)
            probe_sessions_deleted = await _delete_in_batches(
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
            grid_queue_tickets_deleted = await _delete_in_batches(
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

        # ConfigAuditLog
        audit_days: int = self._settings.get("retention.audit_log_days")
        if audit_days > 0:
            cutoff = now - timedelta(days=audit_days)
            audit_deleted = await _delete_in_batches(
                db,
                model=ConfigAuditLog,
                timestamp_column=ConfigAuditLog.changed_at,
                cutoff=cutoff,
            )

        outbox_days: int = self._settings.get("retention.agent_reconfigure_outbox_days")
        if outbox_days > 0:
            cutoff = now - timedelta(days=outbox_days)
            agent_reconfigure_outbox_deleted = await _delete_in_batches(
                db,
                model=AgentReconfigureOutbox,
                timestamp_column=AgentReconfigureOutbox.created_at,
                cutoff=cutoff,
                extra_predicates=(
                    (
                        AgentReconfigureOutbox.delivered_at.is_not(None)
                        | AgentReconfigureOutbox.abandoned_at.is_not(None)
                    ),
                ),
            )

        # DeviceTestDataAuditLog (reuses retention.audit_log_days)
        if audit_days > 0:
            cutoff = now - timedelta(days=audit_days)
            test_data_audit_deleted = await _delete_in_batches(
                db,
                model=DeviceTestDataAuditLog,
                timestamp_column=DeviceTestDataAuditLog.changed_at,
                cutoff=cutoff,
            )
        else:
            test_data_audit_deleted = 0

        # DeviceEvent
        events_days: int = self._settings.get("retention.device_events_days")
        if events_days > 0:
            cutoff = now - timedelta(days=events_days)
            events_deleted = await _delete_in_batches(
                db,
                model=DeviceEvent,
                timestamp_column=DeviceEvent.created_at,
                cutoff=cutoff,
            )

        # HostResourceSample
        host_resource_telemetry_hours: int = self._settings.get("retention.host_resource_telemetry_hours")
        if host_resource_telemetry_hours > 0:
            cutoff = now - timedelta(hours=host_resource_telemetry_hours)
            host_resource_samples_deleted = await _delete_in_batches(
                db,
                model=HostResourceSample,
                timestamp_column=HostResourceSample.recorded_at,
                cutoff=cutoff,
            )

        agent_log_days: int = self._settings.get("retention.agent_log_days")
        if agent_log_days > 0:
            cutoff = now - timedelta(days=agent_log_days)
            # `received_at` is server-clock; `ts` is agent-reported and may be skewed.
            agent_log_entries_deleted = await _delete_in_batches(
                db,
                model=HostAgentLogEntry,
                timestamp_column=HostAgentLogEntry.received_at,
                cutoff=cutoff,
            )

        capacity_snapshots_days: int = self._settings.get("retention.capacity_snapshots_days")
        if capacity_snapshots_days > 0:
            cutoff = now - timedelta(days=capacity_snapshots_days)
            capacity_snapshots_deleted = await _delete_in_batches(
                db,
                model=AnalyticsCapacitySnapshot,
                timestamp_column=AnalyticsCapacitySnapshot.captured_at,
                cutoff=cutoff,
            )

        diagnostic_snapshots_days: int = self._settings.get("retention.diagnostic_snapshots_days")
        if diagnostic_snapshots_days > 0:
            cutoff = now - timedelta(days=diagnostic_snapshots_days)
            diagnostic_snapshots_deleted = await _delete_in_batches(
                db,
                model=DeviceDiagnosticSnapshot,
                timestamp_column=DeviceDiagnosticSnapshot.captured_at,
                cutoff=cutoff,
            )

        logger.info(
            "Data cleanup completed: sessions=%d, probe_sessions=%d, audit_logs=%d, test_data_audit_logs=%d, "
            "device_events=%d, host_resource_samples=%d, agent_log_entries=%d, capacity_snapshots=%d, "
            "diagnostic_snapshots=%d, agent_reconfigure_outbox=%d, grid_queue_tickets=%d",
            sessions_deleted,
            probe_sessions_deleted,
            audit_deleted,
            test_data_audit_deleted,
            events_deleted,
            host_resource_samples_deleted,
            agent_log_entries_deleted,
            capacity_snapshots_deleted,
            diagnostic_snapshots_deleted,
            agent_reconfigure_outbox_deleted,
            grid_queue_tickets_deleted,
        )
        await self._publisher.publish(
            "system.cleanup_completed",
            {
                "sessions_deleted": sessions_deleted,
                "probe_sessions_deleted": probe_sessions_deleted,
                "audit_entries_deleted": audit_deleted,
                "test_data_audit_entries_deleted": test_data_audit_deleted,
                "device_events_deleted": events_deleted,
                "host_resource_samples_deleted": host_resource_samples_deleted,
                "agent_log_entries_deleted": agent_log_entries_deleted,
                "capacity_snapshots_deleted": capacity_snapshots_deleted,
                "diagnostic_snapshots_deleted": diagnostic_snapshots_deleted,
                "agent_reconfigure_outbox_deleted": agent_reconfigure_outbox_deleted,
                "grid_queue_tickets_deleted": grid_queue_tickets_deleted,
            },
        )


class DataCleanupLoop(BackgroundLoop):
    """Background loop that periodically cleans up old data."""

    loop_name = LOOP_NAME
    exit_on_leadership_lost = False  # pre-scaffold: no LeadershipLost handler
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
