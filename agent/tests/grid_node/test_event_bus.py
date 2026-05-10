from __future__ import annotations

import asyncio

import pytest

from agent_app.grid_node.event_bus import EventBus, decode_event_frames, encode_event_frames
from agent_app.grid_node.protocol import EventType, event_envelope


def test_event_frames_round_trip_json_envelope() -> None:
    event = event_envelope(EventType.NODE_STATUS, {"id": "node-1"})
    frames = encode_event_frames(event)
    assert decode_event_frames(frames) == event


@pytest.mark.asyncio
async def test_event_bus_publish_reaches_subscriber() -> None:
    received: list[dict[str, object]] = []
    bus = EventBus(publish_url="inproc://grid-node-test", subscribe_url="inproc://grid-node-test", heartbeat_sec=1.0)
    bus.on_event(lambda event: received.append(event))
    await bus.start()
    await bus.publish(event_envelope(EventType.NODE_DRAIN, {"id": "node-1"}))
    for _ in range(20):
        if received:
            break
        await asyncio.sleep(0.05)
    await bus.stop()
    assert received == [event_envelope(EventType.NODE_DRAIN, {"id": "node-1"})]
