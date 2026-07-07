"""One agent observation per host per tick, feeding every host-scoped concern.

Concerns run only for hosts this sweep pass proved alive: host liveness
(heartbeat evaluation) and appium-node convergence from the same /agent/health
payload, then node health as a cadence-gated stage. Connectivity folds in as a
cadence-gated global stage that runs after the per-host fan-out completes. Later
phases fold telemetry in here with the same cadence gating (see stage_due).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.appium_nodes.services.reconciler import fetch_backoff_until, fetch_desired_rows
from app.core.background_loop import BackgroundLoop
from app.core.metrics_recorders import APPIUM_RECONCILER_CYCLE_FAILURES, APPIUM_RECONCILER_LAST_CYCLE_SECONDS
from app.core.observability import get_logger
from app.hosts.models import Host, HostStatus

if TYPE_CHECKING:
    import uuid
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.protocols import ReconcilerProtocol
    from app.appium_nodes.services.heartbeat import HeartbeatService
    from app.appium_nodes.services.node_health import NodeHealthService
    from app.appium_nodes.services.reconciler_convergence import DesiredRow
    from app.appium_nodes.services_container import AppiumNodeServices
    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory

logger = get_logger(__name__)

LOOP_NAME = "host_sweep"


@dataclass(frozen=True)
class SweepStage:
    """A cross-host pass gated by its own interval setting, run after the fan-out."""

    label: str  # structured-log stage name
    interval_setting: str  # settings key read every cycle for the cadence
    run: Callable[[AsyncSession], Awaitable[None]]


def stage_due(cycle_index: int, *, base_interval: float, stage_interval: float) -> bool:
    """True when a stage with its own interval setting is due on this sweep cycle."""
    divisor = max(1, round(stage_interval / base_interval))
    return cycle_index % divisor == 0


async def run_host_sweep_once(
    db: AsyncSession,
    *,
    heartbeat: HeartbeatService,
    reconciler: ReconcilerProtocol,
    node_health: NodeHealthService,
    settings: SettingsReader,
    session_factory: SessionFactory,
    global_stages: Sequence[SweepStage] = (),
    cycle_index: int = 0,
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
    base_interval = settings.get_float("general.heartbeat_interval_sec")
    node_health_due = stage_due(
        cycle_index,
        base_interval=base_interval,
        stage_interval=settings.get_float("general.node_check_interval_sec"),
    )

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
                # Stage isolation: a convergence failure must not poison other
                # hosts' sweep NOR this host's remaining stages.
                logger.exception("host_sweep_convergence_failed", host_id=str(host_id))
            if node_health_due:
                try:
                    async with session_factory() as nh_db:
                        await node_health.check_host_nodes(nh_db, host_id=host_id)
                except Exception:
                    logger.exception("host_sweep_node_health_failed", host_id=str(host_id))

    await asyncio.gather(*(_sweep_host(host_id) for host_id in host_ids))

    for stage in global_stages:
        if not stage_due(
            cycle_index, base_interval=base_interval, stage_interval=settings.get_float(stage.interval_setting)
        ):
            continue
        # End any open read transaction before a long pass — no snapshot or lock
        # may span agent I/O (repo contract). The statuses each stage reads are the
        # ones this cycle's liveness stage just wrote.
        await db.commit()
        try:
            await stage.run(db)
        except Exception:
            # Stage isolation: one stage's failure must not fail the cycle or skip
            # the stages after it.
            logger.exception("host_sweep_stage_failed", stage=stage.label)


class HostSweepLoop(BackgroundLoop):
    """Leader-owned shared host-observation loop."""

    loop_name = LOOP_NAME
    cycle_failed_message = "host_sweep_cycle_failed"

    def __init__(self, *, services: AppiumNodeServices, global_stages: Sequence[SweepStage] = ()) -> None:
        self._services = services
        self._global_stages = tuple(global_stages)
        self._cycle = 0

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
            node_health=self._services.node_health,
            settings=self._services.settings,
            session_factory=self._services.session_factory,
            global_stages=self._global_stages,
            cycle_index=self._cycle,
        )

    def _on_cycle_end(self, elapsed_seconds: float, interval: float) -> None:
        APPIUM_RECONCILER_LAST_CYCLE_SECONDS.set(elapsed_seconds)
        self._cycle += 1

    def _on_cycle_error(self) -> None:
        APPIUM_RECONCILER_CYCLE_FAILURES.inc()
