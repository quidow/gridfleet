from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.leader import state_store as control_plane_state_store
from app.core.metrics_recorders import record_host_status_push
from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.hosts.models import Host, HostStatus
from app.hosts.service import normalize_capabilities, update_missing_prerequisites_from_health

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.events.protocols import EventPublisher
    from app.hosts.schemas import HostStatusPush

logger = get_logger(__name__)

# One snapshot per host: the latest consolidated status push. Read by the
# host_sweep liveness/convergence stages, host diagnostics, and the resource
# telemetry stage — the single snapshot source (no second fetch path).
HOST_STATUS_NAMESPACE = "status_push.host_status"


class HostStatusPushService:
    """Thin any-worker ingest: liveness stamp + snapshot store. No device row
    locks here — lock-heavy ingest (restart events, convergence, the offline
    cascade) stays in the scheduler's host_sweep."""

    def __init__(self, *, publisher: EventPublisher) -> None:
        self._publisher = publisher

    async def apply_status_push(self, db: AsyncSession, host: Host, push: HostStatusPush) -> None:
        host.last_heartbeat = now_utc()
        if push.agent_version and host.agent_version != push.agent_version:
            host.agent_version = push.agent_version
        if push.capabilities is not None:
            host.capabilities = normalize_capabilities(push.capabilities)
        # After the capabilities update so a pushed top-level list wins over
        # whatever the snapshot carried (mirrors the old health-poll order).
        if push.missing_prerequisites is not None:
            update_missing_prerequisites_from_health(host, push.missing_prerequisites)
        if host.status == HostStatus.offline:
            logger.info("Host %s (%s) is back online", host.hostname, host.ip)
            self._publisher.queue_for_session(
                db,
                "host.status_changed",
                {
                    "host_id": str(host.id),
                    "hostname": host.hostname,
                    "old_status": host.status.value,
                    "new_status": "online",
                },
            )
            host.status = HostStatus.online
        await control_plane_state_store.set_value(
            db,
            HOST_STATUS_NAMESPACE,
            str(host.id),
            {
                "received_at": now_utc().isoformat(),
                "payload": {
                    "appium_processes": push.appium_processes,
                    "host_telemetry": push.host_telemetry,
                    "node_health": push.node_health,
                    "device_health": push.device_health,
                    "device_telemetry": push.device_telemetry,
                    "device_properties": push.device_properties,
                },
            },
        )
        record_host_status_push(host_id=str(host.id))
