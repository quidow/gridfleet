"""Reproducer for bug 7: ``NodeState.expire_reservations`` is defined but
never invoked by ``GridNodeService.run_heartbeat_once`` (or any other reaper
path). A stuck RESERVED slot (e.g., upstream Appium crash mid-session-create)
leaks indefinitely.

See ``docs/superpowers/specs/2026-05-20-agent-bug-audit.md`` (Bug 7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from agent_app.grid_node.config import GridNodeConfig
from agent_app.grid_node.node_state import NodeState
from agent_app.grid_node.protocol import Slot, Stereotype
from agent_app.grid_node.service import GridNodeService

if TYPE_CHECKING:
    import pytest


async def test_heartbeat_clears_stuck_reservations(monkeypatch: pytest.MonkeyPatch) -> None:
    now_holder = [0.0]
    slot = Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "android"}))
    state = NodeState(slots=[slot], now=lambda: now_holder[0])

    config = GridNodeConfig(
        node_id="node-1",
        node_uri="http://node:5555",
        appium_upstream="http://127.0.0.1:4723",
        slots=[slot],
        hub_publish_url="tcp://hub:4442",
        hub_subscribe_url="tcp://hub:4443",
        heartbeat_sec=5.0,
        session_timeout_sec=1800.0,
        proxy_timeout_sec=60.0,
    )

    bus = AsyncMock()
    http_server = AsyncMock()
    service = GridNodeService(config=config, bus=bus, http_server=http_server)
    service.state = state
    service._started = True

    # Simulate an upstream session-create that left the slot stuck RESERVED.
    state.reserve({"platformName": "android"})
    assert state.snapshot().slots[0].state == "RESERVED"

    # Advance well past the 30s reservation TTL and run one heartbeat tick.
    now_holder[0] = 120.0
    monkeypatch.setattr("agent_app.grid_node.service.time.monotonic", lambda: 120.0)
    await service.run_heartbeat_once()

    assert state.snapshot().slots[0].state == "FREE"
