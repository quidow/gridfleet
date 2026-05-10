"""Reconciler-owned Appium port allocator surface."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device
from app.services import appium_node_resource_service as resource_claims
from app.services.node_service_types import NodeManagerError, NodePortConflictError
from app.services.settings_service import settings_service

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

APPIUM_PORT_CAPABILITY = "gridfleet:appiumPort"


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
        .where(AppiumNode.state == NodeState.running, Device.host_id == host_id)
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
    owner_token: str,
) -> int:
    """Reserve exactly one main Appium port as a temporary resource claim."""
    ttl_sec = float(settings_service.get("appium.reservation_ttl_sec"))
    reserved = await resource_claims.reserve(
        db,
        host_id=host_id,
        capability_key=APPIUM_PORT_CAPABILITY,
        start_port=port,
        owner_token=owner_token,
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl_sec),
    )
    if reserved == port:
        return reserved
    await resource_claims.release_temporary(db, host_id=host_id, owner_token=owner_token)
    raise NodePortConflictError(f"Appium port {port} is already reserved on host {host_id}")


release_temporary = resource_claims.release_temporary
release_managed = resource_claims.release_managed
transfer_temporary_to_managed = resource_claims.transfer_temporary_to_managed


__all__ = [
    "APPIUM_PORT_CAPABILITY",
    "candidate_ports",
    "release_managed",
    "release_temporary",
    "reserve_appium_port",
    "transfer_temporary_to_managed",
]
