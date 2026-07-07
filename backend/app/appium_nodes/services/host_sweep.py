"""One agent observation per host per tick, feeding every host-scoped concern.

Phase 1 concerns: host liveness (heartbeat evaluation) and appium-node
convergence, both from the same /agent/health payload. Later phases fold
node_health, device_connectivity, and telemetry in here with per-concern
cadence gating.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.appium_nodes.services.reconciler import fetch_backoff_until, fetch_desired_rows
from app.core.background_loop import BackgroundLoop
from app.core.metrics_recorders import APPIUM_RECONCILER_CYCLE_FAILURES, APPIUM_RECONCILER_LAST_CYCLE_SECONDS
from app.core.observability import get_logger
from app.hosts.models import Host, HostStatus

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.protocols import ReconcilerProtocol
    from app.appium_nodes.services.heartbeat import HeartbeatService
    from app.appium_nodes.services.reconciler_convergence import DesiredRow
    from app.appium_nodes.services_container import AppiumNodeServices
    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory

logger = get_logger(__name__)

LOOP_NAME = "host_sweep"


async def run_host_sweep_once(
    db: AsyncSession,
    *,
    heartbeat: HeartbeatService,
    reconciler: ReconcilerProtocol,
    settings: SettingsReader,
    session_factory: SessionFactory,
) -> None:
    """Fetch and process one shared agent-health observation per host."""
    guard = heartbeat.begin_cycle()
    host_ids = list((await db.execute(select(Host.id).where(Host.status != HostStatus.pending))).scalars().all())
    desired = await fetch_desired_rows(db)
    backoff = await fetch_backoff_until(db)
    rows_by_host: dict[uuid.UUID, list[DesiredRow]] = {}
    for row in desired:
        rows_by_host.setdefault(row.host_id, []).append(row)
    semaphore = asyncio.Semaphore(settings.get_int("appium_reconciler.host_parallelism"))

    async def _sweep_host(host_id: uuid.UUID) -> None:
        async with semaphore:
            try:
                async with session_factory() as host_db:
                    host = await host_db.get(Host, host_id)
                    if host is None:
                        return
                    result = await heartbeat.process_host(host_db, host, guard=guard)
                    host_alive = result.alive and host.status == HostStatus.online
                    host_ip, agent_port = host.ip, host.agent_port
                    await host_db.commit()
            except Exception:
                logger.exception("host_sweep_liveness_failed", host_id=str(host_id))
                return
            if not host_alive:
                return
            try:
                await reconciler.reconcile_host(
                    host_id=host_id,
                    host_ip=host_ip,
                    agent_port=agent_port,
                    rows=rows_by_host.get(host_id, []),
                    backoff_until_by_device=backoff,
                    payload=result.payload or {},
                )
            except Exception:
                # Convergence failure must not poison other hosts' sweep.
                logger.exception("host_sweep_convergence_failed", host_id=str(host_id))

    await asyncio.gather(*(_sweep_host(host_id) for host_id in host_ids))


class HostSweepLoop(BackgroundLoop):
    """Leader-owned shared host-observation loop."""

    loop_name = LOOP_NAME
    cycle_failed_message = "host_sweep_cycle_failed"

    def __init__(self, *, services: AppiumNodeServices) -> None:
        self._services = services

    @property
    def _session_factory(self) -> SessionFactory:
        return self._services.session_factory

    def _interval(self) -> float:
        return self._services.settings.get_float("general.heartbeat_interval_sec")

    async def _run_cycle(self, db: AsyncSession) -> None:
        await run_host_sweep_once(
            db,
            heartbeat=self._services.heartbeat,
            reconciler=self._services.reconciler,
            settings=self._services.settings,
            session_factory=self._services.session_factory,
        )

    def _on_cycle_end(self, elapsed_seconds: float, interval: float) -> None:
        APPIUM_RECONCILER_LAST_CYCLE_SECONDS.set(elapsed_seconds)

    def _on_cycle_error(self) -> None:
        APPIUM_RECONCILER_CYCLE_FAILURES.inc()
