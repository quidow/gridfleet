"""One pushed observation per host per tick: recency liveness, convergence, then
watermark-gated fact folds; a cadence-gated partition-probe diagnostic; cooldown
expiry after the fan-out.

Concerns run only for hosts this sweep pass proved alive: host liveness derives
from status-push recency (evaluate_host), appium-node convergence reads the same
latest pushed snapshot, and per-host observation folds consume stamped push
sections afterward — each fold gated by a per-host stamp watermark so one
observation folds exactly once. The backend keeps a single cadence-gated
reachability probe (probe_host) as a network-partition diagnostic (see
stage_due). After the fan-out the sweep expires stale device cooldowns.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.appium_nodes.services.reconciler import fetch_backoff_until, fetch_desired_rows
from app.core.background_loop import BackgroundLoop, stage_due
from app.core.leader import state_store as control_plane_state_store
from app.core.metrics_recorders import APPIUM_RECONCILER_CYCLE_FAILURES, APPIUM_RECONCILER_LAST_CYCLE_SECONDS
from app.core.observability import get_logger
from app.hosts.liveness import host_online
from app.hosts.models import Host, HostStatus

if TYPE_CHECKING:
    import uuid
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.protocols import ReconcilerProtocol
    from app.appium_nodes.services.heartbeat import HeartbeatService
    from app.appium_nodes.services.reconciler_convergence import DesiredRow
    from app.appium_nodes.services_container import AppiumNodeServices
    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory

logger = get_logger(__name__)

LOOP_NAME = "host_sweep"
OBSERVATION_FOLD_NAMESPACE = "host_sweep.observation_fold"


@dataclass(frozen=True)
class ObservationFold:
    """A per-host fact fold fed by one status-push section.

    Runs only when the section's stamp differs from the stored per-host
    watermark: each push re-sends the agent's latest probe cache, and the
    failure-hysteresis counters downstream count per observation — re-folding
    an unchanged section would multiply them.
    """

    section: str  # push payload key
    fold: Callable[[AsyncSession, uuid.UUID, dict[str, Any]], Awaitable[None]]
    stamp_key: str = "reported_at"


async def _fold_observations(
    db: AsyncSession,
    host_id: uuid.UUID,
    payload: dict[str, Any],
    folds: Sequence[ObservationFold],
) -> None:
    raw = await control_plane_state_store.get_value(db, OBSERVATION_FOLD_NAMESPACE, str(host_id))
    watermarks: dict[str, Any] = raw if isinstance(raw, dict) else {}
    for entry in folds:
        section = payload.get(entry.section)
        if not isinstance(section, dict):
            continue
        stamp = section.get(entry.stamp_key)
        if not isinstance(stamp, str) or stamp == watermarks.get(entry.section):
            continue
        try:
            await entry.fold(db, host_id, section)
        except Exception:
            # Fold isolation (stage-isolation successor): one section's failure
            # must not starve the others. Watermark not advanced — retried next
            # cycle (at-least-once; the restart-event ingest accepts the same).
            logger.exception("host_sweep_fold_failed", section=entry.section, host_id=str(host_id))
            continue
        watermarks[entry.section] = stamp
        await control_plane_state_store.set_value(db, OBSERVATION_FOLD_NAMESPACE, str(host_id), watermarks)
        await db.commit()


async def run_host_sweep_once(
    db: AsyncSession,
    *,
    heartbeat: HeartbeatService,
    reconciler: ReconcilerProtocol,
    settings: SettingsReader,
    session_factory: SessionFactory,
    observation_folds: Sequence[ObservationFold] = (),
    expire_cooldowns: Callable[[AsyncSession], Awaitable[None]] | None = None,
    cycle_index: int = 0,
) -> None:
    """Fetch and process one shared agent-health observation per host."""
    guard = heartbeat.begin_cycle()
    offline_after = settings.get_float("general.host_offline_after_sec")
    host_ids = list((await db.execute(select(Host.id).where(Host.status != HostStatus.pending))).scalars().all())
    desired = await fetch_desired_rows(db, offline_after_sec=offline_after)
    backoff = await fetch_backoff_until(db)
    rows_by_host: dict[uuid.UUID, list[DesiredRow]] = {}
    for row in desired:
        rows_by_host.setdefault(row.host_id, []).append(row)
    semaphore = asyncio.Semaphore(settings.get_int("appium_reconciler.host_parallelism"))
    base_interval = settings.get_float("general.heartbeat_interval_sec")
    probe_due = stage_due(
        cycle_index,
        base_interval=base_interval,
        stage_interval=settings.get_float("general.partition_probe_interval_sec"),
    )

    async def _sweep_host(host_id: uuid.UUID) -> None:
        async with semaphore:
            try:
                async with session_factory() as host_db:
                    host = await host_db.get(Host, host_id)
                    if host is None:
                        return
                    evaluation = await heartbeat.evaluate_host(host_db, host, guard=guard)
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
            if evaluation.payload is not None:
                try:
                    await reconciler.reconcile_host(
                        host_id=host_id,
                        host_ip=host_ip,
                        agent_port=agent_port,
                        rows=rows_by_host.get(host_id, []),
                        backoff_until_by_device=backoff,
                        payload=evaluation.payload,
                    )
                except Exception:
                    # Stage isolation: a convergence failure must not poison other
                    # hosts' sweep NOR this host's remaining stages.
                    logger.exception("host_sweep_convergence_failed", host_id=str(host_id))
            if evaluation.payload is not None and observation_folds:
                try:
                    async with session_factory() as fold_db:
                        await _fold_observations(fold_db, host_id, evaluation.payload, observation_folds)
                except Exception:
                    logger.exception("host_sweep_fold_pass_failed", host_id=str(host_id))

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
        observation_folds: Sequence[ObservationFold] = (),
        expire_cooldowns: Callable[[AsyncSession], Awaitable[None]] | None = None,
    ) -> None:
        self._services = services
        self._observation_folds = tuple(observation_folds)
        self._expire_cooldowns = expire_cooldowns
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
            settings=self._services.settings,
            session_factory=self._services.session_factory,
            observation_folds=self._observation_folds,
            expire_cooldowns=self._expire_cooldowns,
            cycle_index=self._cycle,
        )

    def _on_cycle_end(self, elapsed_seconds: float, interval: float) -> None:
        APPIUM_RECONCILER_LAST_CYCLE_SECONDS.set(elapsed_seconds)
        self._cycle += 1

    def _on_cycle_error(self) -> None:
        APPIUM_RECONCILER_CYCLE_FAILURES.inc()
