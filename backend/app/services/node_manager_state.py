from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device, DeviceAvailabilityStatus
from app.services.device_availability import ready_device_availability_status, set_device_availability_status
from app.services.event_bus import event_bus
from app.services.settings_service import settings_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def allocate_port(db: AsyncSession) -> int:
    return (await candidate_ports(db))[0]


async def candidate_ports(
    db: AsyncSession,
    *,
    preferred_port: int | None = None,
    exclude_ports: set[int] | None = None,
) -> list[int]:
    stmt = select(AppiumNode.port).where(AppiumNode.state == NodeState.running)
    result = await db.execute(stmt)
    used_ports = {row[0] for row in result.all()}
    excluded = exclude_ports or set()
    start_port = settings_service.get("appium.port_range_start")
    end_port = settings_service.get("appium.port_range_end")

    def is_available(port: int) -> bool:
        return start_port <= port <= end_port and port not in used_ports and port not in excluded

    ports: list[int] = []
    if preferred_port is not None and is_available(preferred_port):
        ports.append(preferred_port)

    for port in range(start_port, end_port + 1):
        if port == preferred_port:
            continue
        if is_available(port):
            ports.append(port)

    if ports:
        return ports

    from app.services.node_manager_types import NodeManagerError

    raise NodeManagerError("No free ports available in the configured range")


def upsert_node(
    db: AsyncSession,
    device: Device,
    port: int,
    pid: int | None,
    active_connection_target: str | None,
) -> AppiumNode:
    if device.appium_node:
        node = device.appium_node
        node.port = port
        node.grid_url = settings_service.get("grid.hub_url")
        node.pid = pid
        node.active_connection_target = active_connection_target
        node.state = NodeState.running
    else:
        node = AppiumNode(
            device_id=device.id,
            port=port,
            grid_url=settings_service.get("grid.hub_url"),
            pid=pid,
            active_connection_target=active_connection_target,
            state=NodeState.running,
        )
        db.add(node)
    device.appium_node = node
    return node


async def mark_node_started(
    db: AsyncSession,
    device: Device,
    *,
    port: int,
    pid: int | None,
    active_connection_target: str | None = None,
) -> AppiumNode:
    node = upsert_node(db, device, port, pid, active_connection_target)
    next_status = await ready_device_availability_status(db, device)
    await set_device_availability_status(device, next_status)
    await event_bus.publish(
        "node.state_changed",
        {
            "device_id": str(device.id),
            "device_name": device.name,
            "old_state": "stopped",
            "new_state": "running",
            "port": port,
        },
    )
    await db.commit()
    await db.refresh(node)
    return node


async def mark_node_stopped(db: AsyncSession, device: Device) -> AppiumNode:
    node = device.appium_node
    assert node is not None
    node.state = NodeState.stopped
    node.pid = None
    node.active_connection_target = None
    await set_device_availability_status(device, DeviceAvailabilityStatus.offline)
    await event_bus.publish(
        "node.state_changed",
        {
            "device_id": str(device.id),
            "device_name": device.name,
            "old_state": "running",
            "new_state": "stopped",
        },
    )
    await db.commit()
    await db.refresh(node)
    return node
