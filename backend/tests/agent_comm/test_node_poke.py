from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from app.agent_comm.node_poke import poke_node_refresh
from app.core.errors import AgentUnreachableError
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

SETTINGS = FakeSettingsReader()
CIRCUIT_BREAKER = Mock()
POOL = Mock()


async def test_poke_node_refresh_fires_and_swallows(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The poke resolves the device's host, attempts the fire-and-forget
    refresh, and swallows any failure without raising."""
    device = await create_device(db_session, host_id=db_host.id, name="poke-target")
    poke = AsyncMock(side_effect=AgentUnreachableError(db_host.ip, "offline"))
    monkeypatch.setattr("app.agent_comm.node_poke.agent_operations.agent_nodes_refresh", poke)

    await poke_node_refresh(
        db_session, device.id, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus, pool=POOL
    )

    poke.assert_awaited_once_with(db_host.ip, db_host.agent_port, pool=POOL, circuit_breaker=CIRCUIT_BREAKER)


async def test_poke_node_refresh_no_host_is_a_noop(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device with no resolvable host must not attempt any agent call."""
    poke = AsyncMock()
    monkeypatch.setattr("app.agent_comm.node_poke.agent_operations.agent_nodes_refresh", poke)

    await poke_node_refresh(
        db_session, uuid.uuid4(), settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus
    )

    poke.assert_not_awaited()
