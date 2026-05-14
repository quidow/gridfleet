"""Reconciler-owned Appium port allocator surface."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select, text

from app.appium_nodes.exceptions import NodeManagerError, NodePortConflictError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import resource_service as resource_claims
from app.metrics_recorders import APPIUM_RECONCILER_ALLOCATION_COLLISIONS
from app.models.device import Device
from app.settings import settings_service

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

APPIUM_PORT_CAPABILITY = resource_claims.INTERNAL_APPIUM_PORT_CAPABILITY


async def candidate_ports(
    db: AsyncSession,
    *,
    host_id: uuid.UUID,
    preferred_port: int | None = None,
    exclude_ports: set[int] | None = None,
) -> list[int]:
    """Return free main Appium ports for one host, sorted ascending."""
    stmt = (
        select(AppiumNode.port)
        .join(Device, Device.id == AppiumNode.device_id)
        .where(
            Device.host_id == host_id,
            (
                (AppiumNode.pid.is_not(None) & AppiumNode.active_connection_target.is_not(None))
                | (AppiumNode.desired_state == AppiumDesiredState.running)
            ),
        )
    )
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

    raise NodeManagerError("No free ports available in the configured range")


async def reserve_appium_port(
    db: AsyncSession,
    *,
    host_id: uuid.UUID,
    port: int,
    node_id: uuid.UUID,
) -> int:
    """Reserve exactly one main Appium port for a node."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(CAST(:host_id AS text) || ':' || :capability_key, 0))"),
        {"host_id": str(host_id), "capability_key": APPIUM_PORT_CAPABILITY},
    )
    await resource_claims.release_capability(db, node_id=node_id, capability_key=APPIUM_PORT_CAPABILITY)
    reserved = await resource_claims.reserve(
        db,
        host_id=host_id,
        capability_key=APPIUM_PORT_CAPABILITY,
        start_port=port,
        node_id=node_id,
    )
    if reserved == port:
        return reserved
    await resource_claims.release_capability(db, node_id=node_id, capability_key=APPIUM_PORT_CAPABILITY)
    APPIUM_RECONCILER_ALLOCATION_COLLISIONS.inc()
    raise NodePortConflictError(f"Appium port {port} is already reserved on host {host_id}")


release_managed = resource_claims.release_managed


__all__ = [
    "APPIUM_PORT_CAPABILITY",
    "candidate_ports",
    "release_managed",
    "reserve_appium_port",
]
