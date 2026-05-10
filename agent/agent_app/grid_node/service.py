from __future__ import annotations

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

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self._bus.publish(event_envelope(EventType.NODE_ADDED, self._node_payload()))

    async def stop(self) -> None:
        if not self._started:
            return
        await self._bus.publish(event_envelope(EventType.NODE_REMOVED, {"nodeId": self.config.node_id}))
        self._started = False

    async def run_heartbeat_once(self) -> None:
        await self._bus.publish(event_envelope(EventType.NODE_STATUS, self._node_payload()))

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
