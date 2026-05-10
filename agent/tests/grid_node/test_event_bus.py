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


@pytest.mark.asyncio
async def test_event_bus_records_failed_publish_and_recreates_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = EventBus(publish_url="inproc://reconnect", subscribe_url="inproc://reconnect", heartbeat_sec=0.1)
    await bus.start()
    original_socket = bus.publish_socket

    async def fail_once(_frames: list[bytes]) -> None:
        raise RuntimeError("send failed")

    monkeypatch.setattr(bus, "_send_multipart", fail_once)
    with pytest.raises(RuntimeError):
        await bus.publish({"type": "NODE_STATUS", "data": {}})
    assert bus.last_publish_ok_at is None
    assert bus.last_publish_failed_at is not None
    assert bus.publish_failures == 1
    assert bus.publish_socket is not original_socket
    await bus.stop()
