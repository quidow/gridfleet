from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

from agent_app.grid_node.node_state import NodeState
from agent_app.grid_node.protocol import EventType, event_envelope

if TYPE_CHECKING:
    from agent_app.grid_node.config import GridNodeConfig


class EventPublisher(Protocol):
    async def publish(self, event: dict[str, object]) -> None: ...


class GridNodeService:
    def __init__(self, *, config: GridNodeConfig, bus: EventPublisher) -> None:
        self.config = config
        self.state = NodeState(slots=config.slots, now=lambda: 0.0)
        self._bus = bus
        self._started = False
        self._requested_stop = False
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self._bus.publish(event_envelope(EventType.NODE_ADDED, self._node_payload()))

    async def stop(self) -> None:
        if self._heartbeat_task is not None and asyncio.current_task() is self._heartbeat_task:
            raise RuntimeError("stop must be called by the service owner")
        if not self._started:
            return
        await self._bus.publish(event_envelope(EventType.NODE_REMOVED, {"nodeId": self.config.node_id}))
        self._started = False

    async def run_heartbeat_once(self) -> None:
        if self.state.snapshot().drain:
            self._requested_stop = True
            await self._bus.publish(event_envelope(EventType.NODE_DRAIN_COMPLETE, {"nodeId": self.config.node_id}))
            return
        await self._bus.publish(event_envelope(EventType.NODE_STATUS, self._node_payload()))

    def snapshot(self) -> dict[str, object]:
        return {"requested_stop": self._requested_stop, "started": self._started}

    async def call_stop_from_heartbeat_for_test(self) -> None:
        self._heartbeat_task = asyncio.current_task()
        try:
            await self.stop()
        finally:
            self._heartbeat_task = None

    def _node_payload(self) -> dict[str, object]:
        snapshot = self.state.snapshot()
        return {
            "nodeId": self.config.node_id,
            "externalUri": self.config.node_uri,
            "slots": [
                {
                    "id": slot.slot_id,
                    "state": slot.state,
                    "sessionId": slot.session_id,
                }
                for slot in snapshot.slots
            ],
        }
