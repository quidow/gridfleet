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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from app.config import reconciler_convergence_enabled
from app.database import async_session
from app.metrics_recorders import (
    APPIUM_RECONCILER_CYCLE_FAILURES,
    APPIUM_RECONCILER_HOST_CYCLE_SECONDS,
    APPIUM_RECONCILER_LAST_CYCLE_SECONDS,
    APPIUM_RECONCILER_ORPHANS_STOPPED,
    APPIUM_RECONCILER_START_FAILURES,
    APPIUM_RECONCILER_STOP_FAILURES,
)
from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device
from app.models.host import Host, HostStatus
from app.observability import get_logger, observe_background_loop
from app.services import device_locking
from app.services.agent_operations import agent_base_url, agent_health, appium_stop
from app.services.agent_snapshot import parse_running_nodes
from app.services.appium_reconciler_convergence import DesiredRow, ObservedEntry, converge_host_rows
from app.services.control_plane_leader import LeadershipLost, assert_current_leader
from app.services.desired_state_writer import write_desired_state
from app.services.lifecycle_policy_actions import (
    record_reconciler_start_failure_state,
    reset_reconciler_start_failure_state,
)
from app.services.lifecycle_policy_state import state as lifecycle_policy_state
from app.services.node_service import mark_node_started, mark_node_stopped, start_temporary_node, stop_temporary_node
from app.services.node_service_types import TemporaryNodeHandle
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
    `host_id`, `device_connection_target`, `node_port`, `node_state`,
    and optionally `node_desired_state`.
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
        running_rows = [
            r for r in matched_rows if r.get("node_desired_state") == "running" or r.get("node_state") == "running"
        ]
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
                desired = await _fetch_desired_rows(db)
                backoff = await _fetch_backoff_until(db)
            # Agent IO and stops happen outside the DB session — no point holding it open.
            health_by_host = await _reconcile_all(hosts, rows)
            if reconciler_convergence_enabled():
                await _drive_convergence(hosts, desired, backoff, health_by_host=health_by_host)
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
) -> dict[uuid.UUID, dict[str, object]]:
    health_by_host: dict[uuid.UUID, dict[str, object]] = {}

    async def _reconcile(
        *,
        host_id: uuid.UUID,
        host_ip: str,
        agent_port: int,
        db_running_rows: Sequence[dict[str, object]],
    ) -> list[OrphanAppiumNode]:
        async def _fetch_health(*, host: str, agent_port: int) -> dict[str, object]:
            payload = await agent_health(host, agent_port, http_client_factory=httpx.AsyncClient)
            health = payload or {}
            health_by_host[host_id] = health
            return health

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
    return health_by_host


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
        AppiumNode.desired_state,
    ).join(AppiumNode, AppiumNode.device_id == Device.id)
    result = await db.execute(stmt)
    return [
        {
            "host_id": row.host_id,
            "device_connection_target": row.device_connection_target,
            "node_port": row.port,
            "node_state": row.state.value,
            "node_desired_state": row.desired_state.value,
        }
        for row in result.all()
    ]


async def _fetch_desired_rows(db: AsyncSession) -> list[DesiredRow]:
    target_expr = func.coalesce(Device.connection_target, Device.identity_value)
    stmt = (
        select(
            Device.id.label("device_id"),
            Device.host_id,
            AppiumNode.id.label("node_id"),
            target_expr.label("connection_target"),
            AppiumNode.desired_state,
            AppiumNode.desired_port,
            AppiumNode.transition_token,
            AppiumNode.transition_deadline,
            AppiumNode.state,
            AppiumNode.port,
            AppiumNode.pid,
            AppiumNode.active_connection_target,
        )
        .join(AppiumNode, AppiumNode.device_id == Device.id)
        .join(Host, Host.id == Device.host_id)
        .where(Host.status == HostStatus.online)
    )
    rows = (await db.execute(stmt)).all()
    return [
        DesiredRow(
            device_id=row.device_id,
            host_id=row.host_id,
            node_id=row.node_id,
            connection_target=row.connection_target,
            desired_state=row.desired_state.value,
            desired_port=row.desired_port,
            transition_token=row.transition_token,
            transition_deadline=row.transition_deadline,
            state=row.state.value,
            port=row.port,
            pid=row.pid,
            active_connection_target=row.active_connection_target,
        )
        for row in rows
    ]


async def _fetch_backoff_until(db: AsyncSession) -> dict[uuid.UUID, datetime]:
    rows = (await db.execute(select(Device.id, Device.lifecycle_policy_state))).all()
    backoff: dict[uuid.UUID, datetime] = {}
    for device_id, state_json in rows:
        if not isinstance(state_json, dict):
            continue
        raw = state_json.get("backoff_until")
        if not isinstance(raw, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            backoff[device_id] = parsed
        except (TypeError, ValueError):
            continue
    return backoff


async def _drive_convergence(
    hosts: list[dict[str, object]],
    desired: list[DesiredRow],
    backoff_until_by_device: dict[uuid.UUID, datetime],
    *,
    health_by_host: dict[uuid.UUID, dict[str, object]] | None = None,
) -> None:
    semaphore = asyncio.Semaphore(int(settings_service.get("appium_reconciler.host_parallelism")))
    now = datetime.now(UTC)
    rows_by_host: dict[uuid.UUID, list[DesiredRow]] = {}
    active_rows_by_host: dict[uuid.UUID, list[DesiredRow]] = {}
    for row in desired:
        rows_by_host.setdefault(row.host_id, []).append(row)
        backoff_until = backoff_until_by_device.get(row.device_id)
        if backoff_until is not None and backoff_until > now:
            continue
        active_rows_by_host.setdefault(row.host_id, []).append(row)

    async def _reconcile_host(host: dict[str, object]) -> None:
        host_id = host.get("id")
        host_ip = host.get("ip")
        agent_port = host.get("agent_port")
        if not isinstance(host_id, uuid.UUID) or not isinstance(host_ip, str) or not isinstance(agent_port, int):
            return
        rows = rows_by_host.get(host_id, [])
        if not rows:
            return
        async with semaphore:
            cycle_start = time.monotonic()
            try:
                async with async_session() as db:
                    await assert_current_leader(db)
                payload = (health_by_host or {}).get(host_id)
                if payload is None:
                    payload = await agent_health(host_ip, agent_port, http_client_factory=httpx.AsyncClient) or {}
                appium_processes = payload.get("appium_processes") if isinstance(payload, dict) else None
                if not isinstance(appium_processes, dict):
                    return
                running = parse_running_nodes(appium_processes)
                observed = [
                    ObservedEntry(port=entry.port, pid=entry.pid, connection_target=entry.connection_target)
                    for entry in running
                ]
                await _touch_last_observed(rows)
                active_rows = active_rows_by_host.get(host_id, [])
                if not active_rows:
                    return
                await converge_host_rows(
                    host_id=host_id,
                    rows=active_rows,
                    agent_running=observed,
                    now=datetime.now(UTC),
                    start_agent=_make_start_agent(),
                    stop_agent=_make_stop_agent(host_ip, agent_port),
                    write_observed=_write_observed_factory(),
                    clear_token=_clear_token_factory(),
                )
            finally:
                APPIUM_RECONCILER_HOST_CYCLE_SECONDS.labels(host_id=str(host_id)).observe(
                    time.monotonic() - cycle_start
                )

    await asyncio.gather(*(_reconcile_host(host) for host in hosts))


def _make_start_agent() -> Callable[..., Awaitable[dict[str, Any]]]:
    async def _start(*, row: DesiredRow, port: int | None) -> dict[str, Any]:
        async with async_session() as db:
            await assert_current_leader(db)
            device = await _load_device_for_reconciler(db, row.device_id)
            if device is None:
                raise RuntimeError(f"Device {row.device_id} no longer exists")
            try:
                handle = await start_temporary_node(
                    db,
                    device,
                    owner_key=f"device:{row.device_id}",
                    port=port,
                    reuse_existing=False,
                )
                if handle.port <= 0:
                    raise RuntimeError(f"Agent returned invalid Appium port {handle.port} for device {row.device_id}")
            except Exception as exc:
                reason = _classify_start_failure(exc)
                APPIUM_RECONCILER_START_FAILURES.labels(reason=reason).inc()
                await _record_start_failure(row, reason=reason)
                raise
            await _reset_start_failure(row)
            return {
                "port": handle.port,
                "pid": handle.pid,
                "active_connection_target": handle.active_connection_target,
                "allocated_caps": handle.allocated_caps,
            }

    return _start


def _make_stop_agent(host_ip: str, agent_port: int) -> Callable[..., Awaitable[None]]:
    async def _stop(*, row: DesiredRow, port: int | None) -> None:
        if port is None or port <= 0:
            return
        async with async_session() as db:
            await assert_current_leader(db)
            device = await _load_device_for_reconciler(db, row.device_id)
            if device is None:
                return
            handle = TemporaryNodeHandle(
                port=port,
                pid=row.pid,
                active_connection_target=row.active_connection_target,
                agent_base=agent_base_url(host_ip, agent_port),
                owner_key=f"device:{row.device_id}",
            )
            try:
                stopped = await stop_temporary_node(db, device, handle)
            except Exception:
                APPIUM_RECONCILER_STOP_FAILURES.labels(reason="exception").inc()
                raise
            if not stopped:
                APPIUM_RECONCILER_STOP_FAILURES.labels(reason="not_acknowledged").inc()
                raise RuntimeError(f"Agent did not acknowledge Appium stop for device {row.device_id} on port {port}")

    return _stop


def _write_observed_factory() -> Callable[..., Awaitable[None]]:
    async def _write(
        *,
        row: DesiredRow,
        state: str,
        port: int | None,
        pid: int | None,
        active_connection_target: str | None,
        clear_desired_port: bool = False,
        clear_transition: bool = False,
        allocated_caps: object = None,
    ) -> None:
        async with async_session() as db:
            await assert_current_leader(db)
            device = await _load_device_for_reconciler(db, row.device_id)
            if device is None:
                return
            if state == "running":
                await mark_node_started(
                    db,
                    device,
                    port=port or row.port or 0,
                    pid=pid,
                    active_connection_target=active_connection_target,
                    allocated_caps=allocated_caps if isinstance(allocated_caps, dict) else None,
                    clear_transition=clear_transition,
                )
                return
            else:
                await mark_node_stopped(db, device)
            if clear_desired_port or clear_transition:
                device = await _lock_device_for_reconciler(db, row.device_id)
                if device is None or device.appium_node is None:
                    return
                node = device.appium_node
                target = node.desired_state if node.desired_state != NodeState.error else NodeState.stopped
                desired_port = None if clear_desired_port else node.desired_port
                transition_token = None if clear_transition else node.transition_token
                transition_deadline = None if clear_transition else node.transition_deadline
                await write_desired_state(
                    db,
                    node=node,
                    target=target,
                    caller="appium_reconciler",
                    desired_port=desired_port,
                    transition_token=transition_token,
                    transition_deadline=transition_deadline,
                )
                await db.commit()

    return _write


def _clear_token_factory() -> Callable[..., Awaitable[None]]:
    async def _clear(*, row: DesiredRow, reason: str) -> None:
        async with async_session() as db:
            await assert_current_leader(db)
            await _clear_transition_token(db, row)

    return _clear


async def _load_device_for_reconciler(db: AsyncSession, device_id: uuid.UUID) -> Device | None:
    result = await db.execute(
        select(Device)
        .where(Device.id == device_id)
        .options(selectinload(Device.host), selectinload(Device.appium_node))
    )
    return result.scalar_one_or_none()


async def _lock_device_for_reconciler(db: AsyncSession, device_id: uuid.UUID) -> Device | None:
    return await device_locking.lock_device(db, device_id)


async def _clear_transition_token(db: AsyncSession, row: DesiredRow) -> None:
    device = await _lock_device_for_reconciler(db, row.device_id)
    if device is None or device.appium_node is None:
        return
    node = device.appium_node
    await write_desired_state(
        db,
        node=node,
        target=node.desired_state if node.desired_state != NodeState.error else NodeState.stopped,
        caller="appium_reconciler",
        desired_port=node.desired_port,
    )
    await db.commit()


async def _touch_last_observed(rows: list[DesiredRow]) -> None:
    if not rows:
        return
    async with async_session() as db:
        await assert_current_leader(db)
        node_ids = [row.node_id for row in rows]
        await db.execute(
            update(AppiumNode).where(AppiumNode.id.in_(node_ids)).values(last_observed_at=datetime.now(UTC))
        )
        await db.commit()


def _classify_start_failure(exc: Exception) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        text = exc.response.text.lower() if exc.response is not None else ""
        if "already_running" in text:
            return "already_running"
        if "port" in text or (exc.response is not None and exc.response.status_code == 409):
            return "port_occupied"
    if isinstance(exc, httpx.HTTPError):
        return "http_error"
    return "http_error"


async def _record_start_failure(row: DesiredRow, *, reason: str) -> None:
    threshold = int(settings_service.get("appium_reconciler.start_failure_threshold"))
    backoff_seconds = int(settings_service.get("appium.startup_timeout_sec")) * 4
    async with async_session() as db:
        await assert_current_leader(db)
        device = await _lock_device_for_reconciler(db, row.device_id)
        if device is None:
            return
        current = lifecycle_policy_state(device)
        attempts = int(current.get("recovery_backoff_attempts", 0)) + 1
        backoff_until = None
        if attempts >= threshold:
            backoff_until = (datetime.now(UTC) + timedelta(seconds=backoff_seconds)).isoformat()
        record_reconciler_start_failure_state(
            device,
            reason=reason,
            attempts=attempts,
            backoff_until=backoff_until,
        )
        await db.commit()


async def _reset_start_failure(row: DesiredRow) -> None:
    async with async_session() as db:
        await assert_current_leader(db)
        device = await _lock_device_for_reconciler(db, row.device_id)
        if device is None:
            return
        current = lifecycle_policy_state(device)
        if not current.get("recovery_backoff_attempts") and not current.get("backoff_until"):
            return
        reset_reconciler_start_failure_state(device)
        await db.commit()


async def run_one_cycle_for_test() -> None:
    async with async_session() as db:
        hosts = await _fetch_online_hosts(db)
        rows = await _fetch_node_rows(db)
        desired = await _fetch_desired_rows(db)
        backoff = await _fetch_backoff_until(db)
    health_by_host = await _reconcile_all(hosts, rows)
    if reconciler_convergence_enabled():
        await _drive_convergence(hosts, desired, backoff, health_by_host=health_by_host)
