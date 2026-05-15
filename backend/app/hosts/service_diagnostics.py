from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agent_comm import circuit_breaker as agent_circuit_breaker_module
from app.appium_nodes.models import AppiumNode
from app.core.leader import state_store as control_plane_state_store
from app.devices.models import Device, DeviceEvent, DeviceEventType
from app.hosts.models import Host
from app.hosts.schemas import (
    HostAppiumProcessesRead,
    HostCircuitBreakerRead,
    HostDiagnosticsNodeRead,
    HostDiagnosticsRead,
    HostRecoveryEventRead,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

APPIUM_PROCESSES_NAMESPACE = "heartbeat.appium_processes"
RECENT_RECOVERY_EVENT_FETCH_LIMIT = 50
RECENT_RECOVERY_EVENT_LIMIT = 10


class ProcessNodePayload(TypedDict, total=False):
    port: int
    pid: int
    connection_target: str
    platform_id: str


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


def _is_agent_local_recovery_event(event: DeviceEvent) -> bool:
    details = event.details if isinstance(event.details, dict) else {}
    if event.event_type == DeviceEventType.node_crash:
        return details.get("source") == "agent_local_restart"
    if event.event_type == DeviceEventType.node_restart:
        return details.get("recovered_from") == "agent_auto_restart"
    return False


def _normalize_recovery_process(value: object) -> str:
    if value == "grid_relay":
        return "grid_relay"
    return "appium"


def _normalize_occurred_at(value: object, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        with_value = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(with_value)
        except ValueError:
            return fallback
    return fallback


def _normalize_process_nodes(raw_nodes: object) -> list[ProcessNodePayload]:
    if not isinstance(raw_nodes, list):
        return []

    normalized: list[ProcessNodePayload] = []
    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            continue
        port = _coerce_int(raw_node.get("port"))
        if port is None:
            continue
        payload: ProcessNodePayload = {"port": port}
        pid = _coerce_int(raw_node.get("pid"))
        if pid is not None:
            payload["pid"] = pid
        connection_target = raw_node.get("connection_target")
        if isinstance(connection_target, str):
            payload["connection_target"] = connection_target
        platform_id = raw_node.get("platform_id")
        if isinstance(platform_id, str):
            payload["platform_id"] = platform_id
        normalized.append(payload)
    return normalized


async def _build_appium_processes_snapshot(db: AsyncSession, host: Host) -> HostAppiumProcessesRead:
    raw_snapshot = await control_plane_state_store.get_value(db, APPIUM_PROCESSES_NAMESPACE, str(host.id))
    if not isinstance(raw_snapshot, dict):
        return HostAppiumProcessesRead()

    normalized_nodes = _normalize_process_nodes(raw_snapshot.get("running_nodes"))
    ports = [node["port"] for node in normalized_nodes]
    nodes_by_port: dict[int, AppiumNode] = {}
    if ports:
        stmt = (
            select(AppiumNode)
            .join(Device)
            .where(Device.host_id == host.id, AppiumNode.port.in_(ports))
            .options(selectinload(AppiumNode.device))
        )
        result = await db.execute(stmt)
        nodes_by_port = {node.port: node for node in result.scalars().all()}

    running_nodes: list[HostDiagnosticsNodeRead] = []
    for node_payload in normalized_nodes:
        port = node_payload["port"]
        matched_node = nodes_by_port.get(port)
        device = matched_node.device if matched_node is not None else None
        running_nodes.append(
            HostDiagnosticsNodeRead(
                port=port,
                pid=node_payload.get("pid"),
                connection_target=node_payload.get("connection_target")
                or (device.connection_target if device is not None else None),
                platform_id=node_payload.get("platform_id") or (device.platform_id if device is not None else None),
                managed=matched_node is not None,
                node_id=matched_node.id if matched_node is not None else None,
                node_state=("running" if matched_node.observed_running else "stopped")
                if matched_node is not None
                else None,
                device_id=device.id if device is not None else None,
                device_name=device.name if device is not None else None,
            )
        )

    reported_at = raw_snapshot.get("reported_at")
    normalized_reported_at: datetime | None = None
    if isinstance(reported_at, datetime):
        normalized_reported_at = reported_at
    elif isinstance(reported_at, str):
        with_value = reported_at.replace("Z", "+00:00")
        try:
            normalized_reported_at = datetime.fromisoformat(with_value)
        except ValueError:
            normalized_reported_at = None
    return HostAppiumProcessesRead(
        reported_at=normalized_reported_at,
        running_nodes=running_nodes,
    )


async def _list_recent_recovery_events(db: AsyncSession, host: Host) -> list[HostRecoveryEventRead]:
    stmt = (
        select(DeviceEvent, Device)
        .join(Device, DeviceEvent.device_id == Device.id)
        .where(
            Device.host_id == host.id,
            DeviceEvent.event_type.in_((DeviceEventType.node_crash, DeviceEventType.node_restart)),
        )
        .order_by(DeviceEvent.created_at.desc())
        .limit(RECENT_RECOVERY_EVENT_FETCH_LIMIT)
    )
    result = await db.execute(stmt)

    recent_events: list[HostRecoveryEventRead] = []
    for event, device in result.all():
        if not _is_agent_local_recovery_event(event):
            continue
        details = event.details if isinstance(event.details, dict) else {}
        default_kind = "restart_succeeded" if event.event_type == DeviceEventType.node_restart else "crash_detected"
        kind = details.get("kind") if isinstance(details.get("kind"), str) else default_kind
        recent_events.append(
            HostRecoveryEventRead(
                id=event.id,
                device_id=device.id,
                device_name=device.name,
                event_type=event.event_type.value,
                process=_normalize_recovery_process(details.get("process")),
                kind=kind,
                sequence=_coerce_int(details.get("sequence")),
                port=_coerce_int(details.get("port")),
                pid=_coerce_int(details.get("pid")),
                attempt=_coerce_int(details.get("attempt")),
                delay_sec=_coerce_int(details.get("delay_sec")),
                exit_code=_coerce_int(details.get("exit_code")),
                will_restart=details.get("will_restart") if isinstance(details.get("will_restart"), bool) else None,
                occurred_at=_normalize_occurred_at(details.get("occurred_at"), event.created_at),
                recorded_at=event.created_at,
            )
        )
        if len(recent_events) >= RECENT_RECOVERY_EVENT_LIMIT:
            break
    return recent_events


async def get_host_diagnostics(db: AsyncSession, host: Host | UUID) -> HostDiagnosticsRead | None:
    resolved_host = host if isinstance(host, Host) else await db.get(Host, host)
    if resolved_host is None:
        return None

    return HostDiagnosticsRead(
        host_id=resolved_host.id,
        circuit_breaker=HostCircuitBreakerRead.model_validate(
            agent_circuit_breaker_module.agent_circuit_breaker.public_snapshot(resolved_host.ip)
        ),
        appium_processes=await _build_appium_processes_snapshot(db, resolved_host),
        recent_recovery_events=await _list_recent_recovery_events(db, resolved_host),
    )
