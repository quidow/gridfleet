"""Host factory for the demo seeding scenarios."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from app.hosts.models import Host, HostStatus, OSType

if TYPE_CHECKING:
    from app.seeding.context import SeedContext


def make_host(
    ctx: SeedContext,
    *,
    hostname: str,
    ip: str,
    os_type: OSType,
    status: HostStatus = HostStatus.online,
    last_heartbeat_offset: timedelta | None = timedelta(seconds=-5),
    agent_port: int = 5100,
    agent_version: str | None = "1.5.0",
    capabilities: dict[str, Any] | None = None,
) -> Host:
    """Build an unflushed Host with the given parameters.

    `last_heartbeat_offset` is relative to `ctx.now`. Pass a negative `timedelta`
    for "seen N minutes ago" and `None` to leave heartbeat unset.
    """
    host = Host(
        hostname=hostname,
        ip=ip,
        os_type=os_type,
        status=status,
        agent_port=agent_port,
        agent_version=agent_version,
        capabilities=capabilities,
    )
    if last_heartbeat_offset is not None:
        host.last_heartbeat = ctx.now + last_heartbeat_offset
    return host
