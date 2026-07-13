from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any

from app.core.leader import state_store as control_plane_state_store
from app.core.metrics_recorders import HOST_PUSH_OBSERVATION_FAILURES, record_host_status_push
from app.core.observability import get_logger
from app.core.observation_revision import next_observation_revision
from app.core.timeutil import now_utc
from app.hosts.models import Host
from app.hosts.service import normalize_capabilities, update_missing_prerequisites_from_health

logger = get_logger(__name__)

if TYPE_CHECKING:
    import uuid
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SessionFactory
    from app.events.protocols import EventPublisher
    from app.hosts.schemas import HostStatusPush

# One snapshot per host: the latest consolidated status push. Read by the
# host_sweep liveness/convergence stages, host diagnostics, and the resource
# telemetry stage — the single snapshot source (no second fetch path).
HOST_STATUS_NAMESPACE = "status_push.host_status"

# The two moved health folds (node_health, device_health) that carry an
# ingest-stamped observation revision for the two-axis write-ordering guard.
GUARDED_SECTIONS = ("node_health", "device_health")

# Key under which the ingest-time revision is stamped onto each guarded snapshot
# section. The inline folds read it and pass it to the guarded health writers.
OBSERVATION_REVISION_KEY = "observation_revision"


@dataclass(frozen=True)
class ObservationFold:
    """One push section folded into durable device or host facts."""

    section: str
    fold: Callable[[AsyncSession, uuid.UUID, dict[str, Any]], Awaitable[None]]


def push_sections(push: HostStatusPush) -> dict[str, Any]:
    return {
        "appium_processes": push.appium_processes,
        "host_telemetry": push.host_telemetry,
        "node_health": push.node_health,
        "device_health": push.device_health,
        "device_telemetry": push.device_telemetry,
        "device_properties": push.device_properties,
    }


class HostStatusPushService:
    """Persist liveness first, then contain push-time observation processing."""

    def __init__(
        self,
        *,
        publisher: EventPublisher,
        session_factory: SessionFactory | None = None,
        observation_folds: tuple[ObservationFold, ...] = (),
        converge_host: Callable[..., Awaitable[None]] | None = None,
        ingest_restart_events: Callable[[AsyncSession, Host, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._publisher = publisher
        self._session_factory = session_factory
        self._observation_folds = observation_folds
        self._converge_host = converge_host
        self._ingest_restart_events = ingest_restart_events

    async def apply_status_push(self, db: AsyncSession, host: Host, push: HostStatusPush) -> dict[str, Any]:
        host.last_heartbeat = now_utc()
        if push.agent_version and host.agent_version != push.agent_version:
            host.agent_version = push.agent_version
        if push.capabilities is not None:
            host.capabilities = normalize_capabilities(push.capabilities)
        # After the capabilities update so a pushed top-level list wins over
        # whatever the snapshot carried (mirrors the old health-poll order).
        if push.missing_prerequisites is not None:
            update_missing_prerequisites_from_health(host, push.missing_prerequisites)
        sections = push_sections(push)
        # Draw the ingest-time revision for each moved health section BEFORE the
        # observation stages run, so a synchronous racer (restart ingest,
        # host-offline cascade) that writes later draws a strictly-greater
        # revision and wins the guard against this (now stale) observation.
        await self._stamp_observation_revisions(db, sections)
        await control_plane_state_store.set_value(
            db,
            HOST_STATUS_NAMESPACE,
            str(host.id),
            {"received_at": now_utc().isoformat(), "payload": sections},
        )
        record_host_status_push(host_id=str(host.id))
        return sections

    @staticmethod
    async def _stamp_observation_revisions(db: AsyncSession, sections: dict[str, Any]) -> None:
        for name in GUARDED_SECTIONS:
            section = sections.get(name)
            if isinstance(section, dict):
                section[OBSERVATION_REVISION_KEY] = await next_observation_revision(db)

    async def process_observations(
        self, *, host_id: uuid.UUID, host_ip: str, agent_port: int, payload: dict[str, Any]
    ) -> None:
        """Run restart ingest, convergence, and folds without raising to the endpoint."""
        if self._session_factory is None:
            return
        if self._ingest_restart_events is not None:
            started = perf_counter()
            try:
                async with self._session_factory() as db:
                    host = await db.get(Host, host_id)
                    if host is not None:
                        await self._ingest_restart_events(db, host, payload)
                        await db.commit()
            except Exception:  # noqa: BLE001 - observation stages must never starve liveness
                HOST_PUSH_OBSERVATION_FAILURES.labels(stage="restart_events").inc()
            self._log_stage("restart_events", host_id, started)
        if self._converge_host is not None:
            started = perf_counter()
            try:
                await self._converge_host(
                    host_id=host_id,
                    host_ip=host_ip,
                    agent_port=agent_port,
                    payload=payload,
                )
            except Exception:  # noqa: BLE001 - observation stages must never starve liveness
                HOST_PUSH_OBSERVATION_FAILURES.labels(stage="convergence").inc()
            self._log_stage("convergence", host_id, started)
        for entry in self._observation_folds:
            section = payload.get(entry.section)
            if not isinstance(section, dict):
                continue
            started = perf_counter()
            try:
                async with self._session_factory() as fold_db:
                    await entry.fold(fold_db, host_id, section)
                    await fold_db.commit()
            except Exception:  # noqa: BLE001 - observation stages must never starve liveness
                HOST_PUSH_OBSERVATION_FAILURES.labels(stage=f"fold:{entry.section}").inc()
            self._log_stage(f"fold:{entry.section}", host_id, started)

    @staticmethod
    def _log_stage(stage: str, host_id: uuid.UUID, started: float) -> None:
        """Per-stage timing for the consolidated push ingest (diagnostic: which
        stage dominates the handler's CPU). Emitted per push per stage."""
        logger.info(
            "status_push_stage",
            stage=stage,
            host_id=str(host_id),
            duration_ms=round((perf_counter() - started) * 1000, 1),
        )
