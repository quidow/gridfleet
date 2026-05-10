"""Leader-owned reconciler for agent-side Appium processes.

Phase 1 scope: orphan cleanup. Walks `/agent/health.appium_processes.running_nodes`
for each online host and stops any agent process that no DB AppiumNode row
in `state == running` claims. Future phases extend this loop to drive
desired-state convergence.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import func, select

from app.database import async_session
from app.metrics_recorders import (
    APPIUM_RECONCILER_CYCLE_FAILURES,
    APPIUM_RECONCILER_LAST_CYCLE_SECONDS,
    APPIUM_RECONCILER_ORPHANS_STOPPED,
)
from app.models.appium_node import AppiumNode
from app.models.device import Device
from app.models.host import Host, HostStatus
from app.observability import get_logger, observe_background_loop
from app.services.agent_operations import agent_base_url, agent_health, appium_stop
from app.services.agent_snapshot import parse_running_nodes
from app.services.control_plane_leader import LeadershipLost, assert_current_leader
from app.services.settings_service import settings_service

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.agent_snapshot import RunningAppiumNode

logger = get_logger(__name__)

FetchHealth = Callable[..., Awaitable[dict[str, object]]]
AppiumStop = Callable[..., Awaitable[object]]
ListOnlineHosts = Callable[[], Awaitable[Sequence[dict[str, object]]]]
ListDbRunningRows = Callable[[], Awaitable[Sequence[dict[str, object]]]]


_ORPHAN_REASON_NO_DB_ROW = "no_db_row"
_ORPHAN_REASON_DB_NOT_RUNNING = "db_state_not_running"
_ORPHAN_REASON_PORT_MISMATCH = "port_mismatch"


@dataclass(frozen=True, slots=True)
class OrphanAppiumNode:
    host_id: uuid.UUID
    port: int
    connection_target: str
    reason: str


ReconcileHost = Callable[..., Awaitable[Sequence[OrphanAppiumNode]]]


def detect_orphans(
    *,
    host_id: uuid.UUID,
    agent_running: Iterable[RunningAppiumNode],
    db_running_rows: Iterable[dict[str, object]],
) -> list[OrphanAppiumNode]:
    """Return entries running on the agent that no DB row claims.

    `db_running_rows` is a list of dicts with keys
    `host_id`, `device_connection_target`, `node_port`, `node_state`.
    Pass all AppiumNode rows for the host (any state) — classification
    needs the full picture to surface stopped-row desyncs as
    `db_state_not_running` rather than `no_db_row`.
    """
    rows_by_target: dict[str, list[dict[str, object]]] = {}
    for db_row in db_running_rows:
        if db_row.get("host_id") != host_id:
            continue
        target = db_row.get("device_connection_target")
        if isinstance(target, str):
            rows_by_target.setdefault(target, []).append(db_row)

    orphans: list[OrphanAppiumNode] = []
    for entry in agent_running:
        matched_rows = rows_by_target.get(entry.connection_target, [])
        if not matched_rows:
            orphans.append(
                OrphanAppiumNode(
                    host_id=host_id,
                    port=entry.port,
                    connection_target=entry.connection_target,
                    reason=_ORPHAN_REASON_NO_DB_ROW,
                )
            )
            continue
        running_rows = [r for r in matched_rows if r.get("node_state") == "running"]
        if any(r.get("node_port") == entry.port for r in running_rows):
            continue
        if running_rows:
            orphans.append(
                OrphanAppiumNode(
                    host_id=host_id,
                    port=entry.port,
                    connection_target=entry.connection_target,
                    reason=_ORPHAN_REASON_PORT_MISMATCH,
                )
            )
            continue
        orphans.append(
            OrphanAppiumNode(
                host_id=host_id,
                port=entry.port,
                connection_target=entry.connection_target,
                reason=_ORPHAN_REASON_DB_NOT_RUNNING,
            )
        )
    return orphans


async def reconcile_host_orphans(
    *,
    host_id: uuid.UUID,
    host_ip: str,
    agent_port: int,
    db_running_rows: Iterable[dict[str, object]],
    fetch_health: FetchHealth,
    appium_stop: AppiumStop,
) -> list[OrphanAppiumNode]:
    """Reconcile a single host: fetch agent snapshot, detect orphans, stop each.

    Returns the list of orphans actually stopped (i.e. excludes those whose
    stop call raised). Failures are logged but never abort the loop — one
    bad host must not stall the rest.
    """
    payload = await fetch_health(host=host_ip, agent_port=agent_port)
    appium_processes = payload.get("appium_processes")
    if not isinstance(appium_processes, dict):
        return []
    agent_running = parse_running_nodes(appium_processes)
    orphans = detect_orphans(
        host_id=host_id,
        agent_running=agent_running,
        db_running_rows=db_running_rows,
    )
    stopped: list[OrphanAppiumNode] = []
    for orphan in orphans:
        try:
            await appium_stop(host=host_ip, agent_port=agent_port, port=orphan.port)
        except Exception:
            logger.warning(
                "appium_reconciler_stop_failed",
                exc_info=True,
                host_id=str(host_id),
                port=orphan.port,
                connection_target=orphan.connection_target,
                reason=orphan.reason,
            )
            continue
        stopped.append(orphan)
        APPIUM_RECONCILER_ORPHANS_STOPPED.labels(reason=orphan.reason).inc()
        logger.info(
            "appium_reconciler_orphan_stopped",
            host_id=str(host_id),
            port=orphan.port,
            connection_target=orphan.connection_target,
            reason=orphan.reason,
        )
    return stopped


async def appium_reconciler_loop_tick(
    *,
    list_online_hosts: ListOnlineHosts,
    list_db_running_rows: ListDbRunningRows,
    reconcile_host: ReconcileHost,
) -> int:
    """Single reconciliation cycle. Returns total orphans stopped across hosts."""
    hosts = await list_online_hosts()
    rows = await list_db_running_rows()
    total_stopped = 0
    for host in hosts:
        host_id = host.get("id")
        host_ip = host.get("ip")
        agent_port = host.get("agent_port")
        if not isinstance(host_id, uuid.UUID) or not isinstance(host_ip, str):
            continue
        if not isinstance(agent_port, int):
            continue
        try:
            stopped = await reconcile_host(
                host_id=host_id,
                host_ip=host_ip,
                agent_port=agent_port,
                db_running_rows=rows,
            )
        except Exception:
            logger.warning("appium_reconciler_host_failed", exc_info=True, host_id=str(host_id))
            continue
        total_stopped += len(stopped)
    return total_stopped


LOOP_NAME = "appium_reconciler_loop"


async def appium_reconciler_loop() -> None:
    """Leader-owned periodic loop. See `backend/app/services/heartbeat.py:695` for the reference shape."""
    while True:
        interval = float(settings_service.get("appium_reconciler.interval_sec"))
        cycle_start = time.monotonic()
        try:
            async with observe_background_loop(LOOP_NAME, interval).cycle(), async_session() as db:
                await assert_current_leader(db)
                hosts = await _fetch_online_hosts(db)
                rows = await _fetch_node_rows(db)
            # Agent IO and stops happen outside the DB session — no point holding it open.
            await _reconcile_all(hosts, rows)
        except LeadershipLost as exc:
            APPIUM_RECONCILER_LAST_CYCLE_SECONDS.set(time.monotonic() - cycle_start)
            logger.error(
                "appium_reconciler_leadership_lost",
                reason=str(exc),
                action="exiting_process_to_prevent_split_brain",
            )
            os._exit(70)
        except Exception:
            APPIUM_RECONCILER_CYCLE_FAILURES.inc()
            logger.exception("appium_reconciler_cycle_failed")
        finally:
            APPIUM_RECONCILER_LAST_CYCLE_SECONDS.set(time.monotonic() - cycle_start)
        await asyncio.sleep(interval)


async def _reconcile_all(
    hosts: list[dict[str, object]],
    rows: list[dict[str, object]],
) -> None:
    async def _reconcile(
        *,
        host_id: uuid.UUID,
        host_ip: str,
        agent_port: int,
        db_running_rows: Sequence[dict[str, object]],
    ) -> list[OrphanAppiumNode]:
        async def _fetch_health(*, host: str, agent_port: int) -> dict[str, object]:
            payload = await agent_health(host, agent_port, http_client_factory=httpx.AsyncClient)
            return payload or {}

        async def _stop(*, host: str, agent_port: int, port: int) -> None:
            response = await appium_stop(
                agent_base_url(host, agent_port),
                host=host,
                agent_port=agent_port,
                port=port,
                http_client_factory=httpx.AsyncClient,
            )
            response.raise_for_status()

        return await reconcile_host_orphans(
            host_id=host_id,
            host_ip=host_ip,
            agent_port=agent_port,
            db_running_rows=db_running_rows,
            fetch_health=_fetch_health,
            appium_stop=_stop,
        )

    async def _list_hosts() -> list[dict[str, object]]:
        return hosts

    async def _list_rows() -> list[dict[str, object]]:
        return rows

    await appium_reconciler_loop_tick(
        list_online_hosts=_list_hosts,
        list_db_running_rows=_list_rows,
        reconcile_host=_reconcile,
    )


async def _fetch_online_hosts(db: AsyncSession) -> list[dict[str, object]]:
    result = await db.execute(select(Host.id, Host.ip, Host.agent_port).where(Host.status == HostStatus.online))
    return [{"id": row.id, "ip": row.ip, "agent_port": row.agent_port} for row in result.all()]


async def _fetch_node_rows(db: AsyncSession) -> list[dict[str, object]]:
    target_expr = func.coalesce(Device.connection_target, Device.identity_value)
    stmt = select(
        Device.host_id,
        target_expr.label("device_connection_target"),
        AppiumNode.port,
        AppiumNode.state,
    ).join(AppiumNode, AppiumNode.device_id == Device.id)
    result = await db.execute(stmt)
    return [
        {
            "host_id": row.host_id,
            "device_connection_target": row.device_connection_target,
            "node_port": row.port,
            "node_state": row.state.value,
        }
        for row in result.all()
    ]
