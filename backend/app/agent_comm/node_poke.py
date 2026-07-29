from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.agent_comm import operations as agent_operations
from app.core.observability import get_logger

if TYPE_CHECKING:
    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class NodeRefreshTarget:
    ip: str
    agent_port: int


async def poke_node_refresh_target(
    target: NodeRefreshTarget,
    *,
    circuit_breaker: CircuitBreakerProtocol,
    pool: AgentHttpPool | None = None,
) -> None:
    try:
        await agent_operations.agent_nodes_refresh(
            target.ip,
            target.agent_port,
            pool=pool,
            circuit_breaker=circuit_breaker,
        )
    except Exception:  # poke is best-effort
        logger.debug("agent nodes refresh poke failed for %s:%d", target.ip, target.agent_port, exc_info=True)
