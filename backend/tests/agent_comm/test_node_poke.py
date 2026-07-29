from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from app.agent_comm.node_poke import NodeRefreshTarget, poke_node_refresh_target
from app.core.errors import AgentUnreachableError

if TYPE_CHECKING:
    import pytest

CIRCUIT_BREAKER = Mock()
POOL = Mock()


async def test_poke_node_refresh_target_needs_no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    target = NodeRefreshTarget(ip="192.0.2.10", agent_port=5100)
    poke = AsyncMock()
    monkeypatch.setattr("app.agent_comm.node_poke.agent_operations.agent_nodes_refresh", poke)

    await poke_node_refresh_target(target, circuit_breaker=CIRCUIT_BREAKER, pool=POOL)

    poke.assert_awaited_once_with("192.0.2.10", 5100, pool=POOL, circuit_breaker=CIRCUIT_BREAKER)


async def test_poke_node_refresh_target_swallows_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    target = NodeRefreshTarget(ip="192.0.2.11", agent_port=5101)
    poke = AsyncMock(side_effect=AgentUnreachableError(target.ip, "offline"))
    monkeypatch.setattr("app.agent_comm.node_poke.agent_operations.agent_nodes_refresh", poke)

    await poke_node_refresh_target(target, circuit_breaker=CIRCUIT_BREAKER)

    poke.assert_awaited_once()
