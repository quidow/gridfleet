from __future__ import annotations

import pytest

from agent_app.grid_node.config import GridNodeConfig
from agent_app.grid_node.protocol import Slot, Stereotype
from agent_app.grid_node.service import GridNodeService


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")

    async def publish(self, event: dict[str, object]) -> None:
        self.calls.append(f"publish:{event['type']}")
        self.events.append(event)


def test_grid_node_config_from_values() -> None:
    slot = Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "Android"}))
    config = GridNodeConfig(
        node_id="node-1",
        node_uri="http://127.0.0.1:5555",
        appium_upstream="http://127.0.0.1:4723",
        slots=[slot],
        hub_publish_url="tcp://127.0.0.1:4442",
        hub_subscribe_url="tcp://127.0.0.1:4443",
        heartbeat_sec=5.0,
        session_timeout_sec=300.0,
        proxy_timeout_sec=30.0,
    )
    assert config.node_id == "node-1"
    assert config.slots == [slot]


@pytest.mark.asyncio
async def test_service_start_and_stop_publish_lifecycle_events() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus)
    await service.start()
    await service.run_heartbeat_once()
    await service.stop()
    assert [event["type"] for event in bus.events] == ["NODE_ADDED", "NODE_STATUS", "NODE_REMOVED"]


@pytest.mark.asyncio
async def test_service_starts_and_stops_event_bus_around_lifecycle_events() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus)
    await service.start()
    await service.stop()
    assert bus.calls == ["start", "publish:NODE_ADDED", "publish:NODE_REMOVED", "stop"]


@pytest.mark.asyncio
async def test_drain_publishes_drain_complete_and_requests_stop() -> None:
    bus = RecordingBus()
    service = GridNodeService(config=_config(), bus=bus)
    await service.start()
    service.state.mark_drain()
    await service.run_heartbeat_once()
    assert bus.events[-1]["type"] == "NODE_DRAIN_COMPLETE"
    assert service.snapshot()["requested_stop"] is True
    await service.stop()


@pytest.mark.asyncio
async def test_stop_called_from_heartbeat_task_raises_runtime_error() -> None:
    service = GridNodeService(config=_config(), bus=RecordingBus())
    await service.start()
    with pytest.raises(RuntimeError, match="owner"):
        await service.call_stop_from_heartbeat_for_test()


def _config() -> GridNodeConfig:
    return GridNodeConfig(
        node_id="node-1",
        node_uri="http://127.0.0.1:5555",
        appium_upstream="http://127.0.0.1:4723",
        slots=[Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "Android"}))],
        hub_publish_url="tcp://127.0.0.1:4442",
        hub_subscribe_url="tcp://127.0.0.1:4443",
        heartbeat_sec=5.0,
        session_timeout_sec=300.0,
        proxy_timeout_sec=30.0,
    )
