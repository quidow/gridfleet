import asyncio
import contextlib
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.errors import AgentCallError
from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device, DeviceOperationalState
from app.models.device_event import DeviceEventType
from app.models.host import Host, HostStatus
from app.observability import get_logger, observe_background_loop
from app.services import (
    appium_node_locking,
    control_plane_state_store,
    device_health,
    device_locking,
    host_service,
    plugin_service,
)
from app.services.agent_operations import agent_health
from app.services.control_plane_leader import LeadershipLost, assert_current_leader
from app.services.device_event_service import record_event
from app.services.device_state import set_operational_state
from app.services.event_bus import queue_device_crashed_event, queue_event_for_session
from app.services.host_diagnostics import APPIUM_PROCESSES_NAMESPACE
from app.services.settings_service import settings_service
from app.type_defs import AsyncTaskFactory

logger = get_logger(__name__)
_background_tasks: set[asyncio.Task[None]] = set()
HEARTBEAT_NAMESPACE = "heartbeat.failure_count"
APPIUM_RESTART_SEQUENCE_NAMESPACE = "heartbeat.appium_restart_sequence"
LOOP_NAME = "heartbeat"
BACKGROUND_TASK_DRAIN_TIMEOUT_SEC = 5.0
APPIUM_RESTART_EVENT_KINDS = frozenset({"crash_detected", "restart_succeeded", "restart_exhausted"})
APPIUM_RESTART_EVENT_PROCESSES = frozenset({"appium", "grid_relay"})


async def _auto_sync_plugins_on_recovery(host_id: uuid.UUID) -> None:
    try:
        async with async_session() as db:
            host = await db.get(Host, host_id)
            if host is None:
                return
            plugins = await plugin_service.list_plugins(db)
            await plugin_service.auto_sync_host_plugins(host, plugins)
    except Exception:
        logger.exception("Automatic plugin sync on recovery failed for host %s", host_id)


def _schedule_background_task(task_fn: AsyncTaskFactory, *args: object) -> None:
    task: asyncio.Task[None] = asyncio.create_task(task_fn(*args))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def shutdown_background_tasks(timeout: float = BACKGROUND_TASK_DRAIN_TIMEOUT_SEC) -> None:
    tasks = {task for task in _background_tasks if not task.done()}
    if not tasks:
        _background_tasks.clear()
        return

    done, pending = await asyncio.wait(tasks, timeout=timeout)
    if pending:
        logger.warning("Cancelling %d heartbeat background task(s) during shutdown", len(pending))
        for task in pending:
            task.cancel()

    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(*done, *pending, return_exceptions=True)
    _background_tasks.clear()


async def _ping_agent(ip: str, port: int) -> dict[str, Any] | None:
    try:
        return await agent_health(ip, port, http_client_factory=httpx.AsyncClient)
    except AgentCallError:
        return None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _restart_process(value: object) -> str:
    if isinstance(value, str) and value in APPIUM_RESTART_EVENT_PROCESSES:
        return value
    return "appium"


def _restart_error_message(kind: str, process: str, exit_code: int | None) -> str:
    exit_detail = f" (code {exit_code})" if exit_code is not None else ""
    process_label = "Grid relay" if process == "grid_relay" else "Appium"
    if kind == "restart_exhausted":
        return f"Agent auto-restart exhausted after {process_label} exit{exit_detail}"
    return f"Agent detected {process_label} exit{exit_detail}"


def _restart_event_observation_changed(
    locked: AppiumNode,
    *,
    observed_id: uuid.UUID,
    observed_port: int,
    observed_state: NodeState,
    observed_pid: int | None,
    observed_active_connection_target: str | None,
) -> bool:
    return (
        locked.id != observed_id
        or locked.port != observed_port
        or locked.state != observed_state
        or locked.pid != observed_pid
        or locked.active_connection_target != observed_active_connection_target
    )


def _normalize_running_nodes(health_data: dict[str, Any]) -> list[dict[str, Any]]:
    process_payload = health_data.get("appium_processes")
    if not isinstance(process_payload, dict):
        return []

    raw_running_nodes = process_payload.get("running_nodes")
    if not isinstance(raw_running_nodes, list):
        return []

    normalized: list[dict[str, Any]] = []
    for raw_node in raw_running_nodes:
        if not isinstance(raw_node, dict):
            continue
        port = _coerce_int(raw_node.get("port"))
        if port is None:
            continue
        node_payload: dict[str, Any] = {"port": port}
        pid = _coerce_int(raw_node.get("pid"))
        if pid is not None:
            node_payload["pid"] = pid
        connection_target = raw_node.get("connection_target")
        if isinstance(connection_target, str):
            node_payload["connection_target"] = connection_target
        platform_id = raw_node.get("platform_id")
        if isinstance(platform_id, str):
            node_payload["platform_id"] = platform_id
        normalized.append(node_payload)
    return normalized


async def _persist_appium_processes_snapshot(db: AsyncSession, host: Host, health_data: dict[str, Any]) -> None:
    await control_plane_state_store.set_value(
        db,
        APPIUM_PROCESSES_NAMESPACE,
        str(host.id),
        {
            "reported_at": datetime.now(UTC).isoformat(),
            "running_nodes": _normalize_running_nodes(health_data),
        },
    )


async def _ingest_appium_restart_events(db: AsyncSession, host: Host, health_data: dict[str, Any]) -> None:
    process_payload = health_data.get("appium_processes")
    if not isinstance(process_payload, dict):
        return

    raw_events = process_payload.get("recent_restart_events")
    if not isinstance(raw_events, list) or not raw_events:
        return

    host_key = str(host.id)
    last_sequence = (
        _coerce_int(await control_plane_state_store.get_value(db, APPIUM_RESTART_SEQUENCE_NAMESPACE, host_key)) or 0
    )

    candidate_events: list[dict[str, Any]] = []
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            continue
        sequence = _coerce_int(raw_event.get("sequence"))
        port = _coerce_int(raw_event.get("port"))
        kind = raw_event.get("kind")
        if sequence is None or sequence <= last_sequence or port is None:
            continue
        if not isinstance(kind, str) or kind not in APPIUM_RESTART_EVENT_KINDS:
            continue
        candidate_events.append(raw_event)

    if not candidate_events:
        return

    candidate_events.sort(key=lambda event: _coerce_int(event.get("sequence")) or 0)
    ports = sorted({port for event in candidate_events if (port := _coerce_int(event.get("port"))) is not None})
    node_stmt = (
        select(AppiumNode)
        .join(Device)
        .where(Device.host_id == host.id, AppiumNode.port.in_(ports))
        .options(selectinload(AppiumNode.device))
    )
    node_result = await db.execute(node_stmt)
    nodes_by_port = {node.port: node for node in node_result.scalars().all()}

    highest_sequence = last_sequence
    for event in candidate_events:
        sequence = _coerce_int(event.get("sequence"))
        port = _coerce_int(event.get("port"))
        if sequence is None or port is None:
            continue
        highest_sequence = max(highest_sequence, sequence)
        node = nodes_by_port.get(port)
        if node is None:
            continue
        observed_id = node.id
        observed_port = node.port
        observed_state = node.state
        observed_pid = node.pid
        observed_active_connection_target = node.active_connection_target
        # Acquire Device → AppiumNode locks before mutating node state.
        device_id = node.device.id
        locked_device = await device_locking.lock_device(db, device_id)
        locked_node = await appium_node_locking.lock_appium_node_for_device(db, device_id)
        if locked_node is None:
            continue
        if _restart_event_observation_changed(
            locked_node,
            observed_id=observed_id,
            observed_port=observed_port,
            observed_state=observed_state,
            observed_pid=observed_pid,
            observed_active_connection_target=observed_active_connection_target,
        ):
            logger.info(
                "Skipping stale local restart event for host %s port %s after node changed",
                host.hostname,
                port,
            )
            continue
        device = locked_device
        kind = str(event["kind"])
        process = _restart_process(event.get("process"))
        attempt = _coerce_int(event.get("attempt")) or 0
        delay_sec = _coerce_int(event.get("delay_sec"))
        exit_code = _coerce_int(event.get("exit_code"))
        pid = _coerce_int(event.get("pid"))
        will_retry = bool(event.get("will_retry"))

        details = {
            "source": "agent_local_restart",
            "sequence": sequence,
            "process": process,
            "kind": kind,
            "attempt": attempt,
            "port": port,
            "will_restart": will_retry,
        }
        if delay_sec is not None:
            details["delay_sec"] = delay_sec
        if exit_code is not None:
            details["exit_code"] = exit_code
        if pid is not None:
            details["pid"] = pid
        occurred_at = event.get("occurred_at")
        if isinstance(occurred_at, str):
            details["occurred_at"] = occurred_at

        if kind == "restart_succeeded":
            if process == "appium" and pid is not None:
                locked_node.pid = pid
            locked_node.consecutive_health_failures = 0
            if process == "appium":
                queue_event_for_session(
                    db,
                    "node.state_changed",
                    {
                        "device_id": str(device.id),
                        "device_name": device.name,
                        "old_state": "error",
                        "new_state": "running",
                        "port": port,
                    },
                )
            await record_event(
                db,
                device.id,
                DeviceEventType.node_restart,
                {
                    **details,
                    "recovered_from": "agent_auto_restart",
                },
            )
            await device_health.apply_node_state_transition(
                db,
                device,
                new_state=NodeState.running if process == "appium" else None,
                health_running=None,
                health_state=None,
                mark_offline=False,
            )
            continue

        error_message = _restart_error_message(kind, process, exit_code)
        queue_event_for_session(
            db,
            "node.crash",
            {
                "device_id": str(device.id),
                "device_name": device.name,
                "error": error_message,
                "will_restart": will_retry,
                "process": process,
            },
        )
        queue_device_crashed_event(
            db,
            device_id=str(device.id),
            device_name=device.name,
            source="agent_restart_exhausted" if kind == "restart_exhausted" else "appium_crash",
            reason=error_message,
            will_restart=will_retry,
            process=process,
        )
        await record_event(
            db,
            device.id,
            DeviceEventType.node_crash,
            {
                **details,
                "error": error_message,
            },
        )
        if process == "grid_relay":
            degraded_state = "relay_restart_exhausted" if kind == "restart_exhausted" else "relay_restarting"
        else:
            degraded_state = "restart_exhausted" if kind == "restart_exhausted" else "restarting"
        await device_health.apply_node_state_transition(
            db,
            device,
            health_running=False,
            health_state=degraded_state,
            mark_offline=False,
            reason=error_message,
        )

    await control_plane_state_store.set_value(db, APPIUM_RESTART_SEQUENCE_NAMESPACE, host_key, highest_sequence)


async def _check_hosts(db: AsyncSession) -> None:
    stmt = select(Host).where(Host.status != HostStatus.pending)
    result = await db.execute(stmt)
    hosts = result.scalars().all()

    for host in hosts:
        host_key = str(host.id)
        health_data = await _ping_agent(host.ip, host.agent_port)
        alive = health_data is not None

        # Fence: drop any writes from a stale leader that lost the advisory lock
        # while we were awaiting _ping_agent. assert_current_leader raises
        # LeadershipLost when another backend now owns the heartbeat row.
        await assert_current_leader(db)

        if alive:
            await control_plane_state_store.delete_value(db, HEARTBEAT_NAMESPACE, host_key)
            # Update agent version if reported
            agent_version = health_data.get("version") if health_data else None
            if agent_version and host.agent_version != agent_version:
                host.agent_version = agent_version
            if host.status != HostStatus.online:
                logger.info("Host %s (%s) is back online", host.hostname, host.ip)
                queue_event_for_session(
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
                _schedule_background_task(_auto_sync_plugins_on_recovery, host.id)
            host.last_heartbeat = datetime.now(UTC)
            if health_data is not None:
                if "missing_prerequisites" in health_data:
                    host_service.update_missing_prerequisites_from_health(
                        host, health_data.get("missing_prerequisites")
                    )
                await _persist_appium_processes_snapshot(db, host, health_data)
                await _ingest_appium_restart_events(db, host, health_data)
        else:
            count = await control_plane_state_store.increment_counter(db, HEARTBEAT_NAMESPACE, host_key)
            logger.warning(
                "Host %s (%s) heartbeat failed (%d/%d)",
                host.hostname,
                host.ip,
                count,
                settings_service.get("general.max_missed_heartbeats"),
            )

            if count >= settings_service.get("general.max_missed_heartbeats") and host.status != HostStatus.offline:
                logger.error("Host %s marked offline after %d missed heartbeats", host.hostname, count)
                queue_event_for_session(
                    db,
                    "host.status_changed",
                    {
                        "host_id": str(host.id),
                        "hostname": host.hostname,
                        "old_status": host.status.value,
                        "new_status": "offline",
                    },
                )
                queue_event_for_session(
                    db,
                    "host.heartbeat_lost",
                    {
                        "host_id": str(host.id),
                        "hostname": host.hostname,
                        "missed_count": count,
                    },
                )
                host.status = HostStatus.offline
                # Mark all devices on this host as offline. lock_devices
                # acquires SELECT FOR UPDATE on each row in id order so
                # operational_state writes serialize against concurrent writers.
                device_id_stmt = select(Device.id).where(Device.host_id == host.id)
                device_ids = list((await db.execute(device_id_stmt)).scalars().all())
                for device in await device_locking.lock_devices(db, device_ids):
                    await record_event(
                        db,
                        device.id,
                        DeviceEventType.connectivity_lost,
                        {"reason": f"Host {host.hostname} offline", "host_id": str(host.id)},
                    )
                    await device_health.update_device_checks(
                        db,
                        device,
                        healthy=False,
                        summary=f"Host {host.hostname} offline",
                    )
                    await set_operational_state(
                        device,
                        DeviceOperationalState.offline,
                        reason=f"Host {host.hostname} offline",
                    )

    await db.commit()


async def heartbeat_loop() -> None:
    """Background loop that pings all host agents."""
    while True:
        interval = float(settings_service.get("general.heartbeat_interval_sec"))
        try:
            async with observe_background_loop(LOOP_NAME, interval).cycle(), async_session() as db:
                await _check_hosts(db)
        except LeadershipLost as exc:
            logger.error(
                "heartbeat_loop_leadership_lost",
                reason=str(exc),
                action="exiting_process_to_prevent_split_brain",
            )
            os._exit(70)
        except Exception:
            logger.exception("Heartbeat check failed")
        await asyncio.sleep(interval)
