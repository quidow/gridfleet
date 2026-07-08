"""Reconciler-owned Appium port allocator surface."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import resource_service as resource_claims
from app.devices.models import Device

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader

APPIUM_PORT_CAPABILITY = resource_claims.INTERNAL_APPIUM_PORT_CAPABILITY


async def candidate_ports(
    db: AsyncSession,
    *,
    host_id: uuid.UUID,
    preferred_port: int | None = None,
    exclude_ports: set[int] | None = None,
    settings: SettingsReader,
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
    start_port = settings.get("appium.port_range_start")
    end_port = settings.get("appium.port_range_end")

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


__all__ = [
    "APPIUM_PORT_CAPABILITY",
    "candidate_ports",
]
