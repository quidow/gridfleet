"""The silence detector: host liveness edges from status-push recency, a
cadence-gated partition-probe diagnostic, and cooldown expiry after the
fan-out.

Everything observational moved to the push ingest path (WS-11.1): restart
ingest, appium-node convergence, and the fact folds run at push time in
HostStatusPushService.process_observations. This sweep notices only what a
push cannot deliver — the host that stopped pushing.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.background_loop import BackgroundLoop, stage_due
from app.core.metrics_recorders import APPIUM_RECONCILER_CYCLE_FAILURES, APPIUM_RECONCILER_LAST_CYCLE_SECONDS
from app.core.observability import get_logger
from app.hosts.liveness import host_online
from app.hosts.models import Host, HostStatus

if TYPE_CHECKING:
    import uuid
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.protocols import ReconcilerProtocol
    from app.appium_nodes.services.heartbeat import HeartbeatService
    from app.appium_nodes.services_container import AppiumNodeServices
    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory

logger = get_logger(__name__)

LOOP_NAME = "host_sweep"
# Plumbing constants (P5): the sweep tick, its stage cadence, and fan-out width
# are not operator policy. The operator-facing knob is host_offline_after_sec.
HOST_SWEEP_INTERVAL_SEC = 15.0
PARTITION_PROBE_INTERVAL_SEC = 60.0
HOST_SWEEP_PARALLELISM = 8


async def run_host_sweep_once(
    db: AsyncSession,
    *,
    heartbeat: HeartbeatService,
    reconciler: ReconcilerProtocol,
    settings: SettingsReader,
    session_factory: SessionFactory,
    expire_cooldowns: Callable[[AsyncSession], Awaitable[None]] | None = None,
    cycle_index: int = 0,
) -> None:
    """Fetch and process one shared agent-health observation per host."""
    guard = heartbeat.begin_cycle()
    offline_after = settings.get_float("general.host_offline_after_sec")
    host_ids = list((await db.execute(select(Host.id).where(Host.status != HostStatus.pending))).scalars().all())
    probe_due = stage_due(
        cycle_index,
        base_interval=HOST_SWEEP_INTERVAL_SEC,
        stage_interval=PARTITION_PROBE_INTERVAL_SEC,
    )
    semaphore = asyncio.Semaphore(HOST_SWEEP_PARALLELISM)

    async def _sweep_host(host_id: uuid.UUID) -> None:
        async with semaphore:
            try:
                async with session_factory() as host_db:
                    host = await host_db.get(Host, host_id)
                    if host is None:
                        return
                    await heartbeat.evaluate_host(host_db, host, guard=guard)
                    host_alive = host_online(host, offline_after_sec=offline_after)
                    host_ip, agent_port = host.ip, host.agent_port
                    await host_db.commit()
            except Exception:
                logger.exception("host_sweep_liveness_failed", host_id=str(host_id))
                return
            if not host_alive:
                return
            if probe_due:
                try:
                    await heartbeat.probe_host(host_id=str(host_id), host_ip=host_ip, agent_port=agent_port)
                except Exception:
                    logger.exception("host_sweep_probe_failed", host_id=str(host_id))

    await asyncio.gather(*(_sweep_host(host_id) for host_id in host_ids))

    if expire_cooldowns is not None:
        # DB-only cleanup with no push section; must run even with zero alive
        # hosts. Every cycle (~10 s) instead of the old 60 s stage — earlier
        # cooldown expiry is benign, and the intent reconciler GCs expired
        # intents on its own tick anyway.
        await db.commit()
        try:
            await expire_cooldowns(db)
        except Exception:
            logger.exception("host_sweep_cooldown_pass_failed")


class HostSweepLoop(BackgroundLoop):
    """Leader-owned shared host-observation loop."""

    loop_name = LOOP_NAME
    cycle_failed_message = "host_sweep_cycle_failed"

    def __init__(
        self,
        *,
        services: AppiumNodeServices,
        expire_cooldowns: Callable[[AsyncSession], Awaitable[None]] | None = None,
    ) -> None:
        self._services = services
        self._expire_cooldowns = expire_cooldowns
        self._cycle = 0

    @property
    def _session_factory(self) -> SessionFactory:
        return self._services.session_factory

    def _interval(self) -> float:
        return HOST_SWEEP_INTERVAL_SEC

    async def _run_cycle(self, db: AsyncSession) -> None:
        await run_host_sweep_once(
            db,
            heartbeat=self._services.heartbeat,
            reconciler=self._services.reconciler,
            settings=self._services.settings,
            session_factory=self._services.session_factory,
            expire_cooldowns=self._expire_cooldowns,
            cycle_index=self._cycle,
        )

    def _on_cycle_end(self, elapsed_seconds: float, interval: float) -> None:
        APPIUM_RECONCILER_LAST_CYCLE_SECONDS.set(elapsed_seconds)
        self._cycle += 1

    def _on_cycle_error(self) -> None:
        APPIUM_RECONCILER_CYCLE_FAILURES.inc()
