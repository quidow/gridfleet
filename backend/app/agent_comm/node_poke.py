from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.agent_comm import operations as agent_operations
from app.core.observability import get_logger
from app.devices.models import Device
from app.hosts.models import Host

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher

logger = get_logger(__name__)


async def poke_node_refresh(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    settings: SettingsReader,
    circuit_breaker: CircuitBreakerProtocol,
    publisher: EventPublisher,
    pool: AgentHttpPool | None = None,
) -> None:
    """Fire-and-forget wake hint: ask ``device_id``'s host agent to re-pull its
    desired node state now. Best-effort — a lost poke costs at most one agent
    poll interval, so any failure is logged and swallowed rather than
    affecting the caller.
    """
    host = (
        await db.execute(select(Host).join(Device, Device.host_id == Host.id).where(Device.id == device_id))
    ).scalar_one_or_none()
    if host is None:
        return
    try:
        await agent_operations.agent_nodes_refresh(
            host.ip, host.agent_port, settings=settings, pool=pool, circuit_breaker=circuit_breaker
        )
    except Exception:  # noqa: BLE001 - poke is best-effort
        logger.debug("agent nodes refresh poke failed for host %s", host.id, exc_info=True)
