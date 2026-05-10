from __future__ import annotations

import asyncio

import pytest

from agent_app.grid_node.event_bus import EventBus, decode_event_frames, encode_event_frames
from agent_app.grid_node.protocol import EventType, event_envelope


class FakeSubscribeSocket:
    def __init__(self, frames: list[list[bytes]]) -> None:
        self._frames = frames

    async def recv_multipart(self) -> list[bytes]:
        await asyncio.sleep(0)
        if not self._frames:
            raise asyncio.CancelledError
        return self._frames.pop(0)


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


@pytest.mark.asyncio
async def test_event_bus_receive_loop_skips_malformed_frames() -> None:
    event = event_envelope(EventType.NODE_STATUS, {"id": "node-1"})
    bus = EventBus(publish_url="inproc://receive-loop", subscribe_url="inproc://receive-loop", heartbeat_sec=1.0)
    received: list[dict[str, object]] = []
    bus.on_event(lambda payload: received.append(payload))
    bus._subscribe_socket = FakeSubscribeSocket([[b"not-json"], encode_event_frames(event)])
    with pytest.raises(asyncio.CancelledError):
        await bus._receive_loop()
    assert received == [event]


@pytest.mark.asyncio
async def test_event_bus_receive_loop_continues_after_handler_error() -> None:
    event = event_envelope(EventType.NODE_DRAIN, {"id": "node-1"})
    bus = EventBus(publish_url="inproc://handler-error", subscribe_url="inproc://handler-error", heartbeat_sec=1.0)
    received: list[dict[str, object]] = []

    def fail(_payload: dict[str, object]) -> None:
        raise RuntimeError("handler failed")

    bus.on_event(fail)
    bus.on_event(lambda payload: received.append(payload))
    bus._subscribe_socket = FakeSubscribeSocket([encode_event_frames(event)])
    with pytest.raises(asyncio.CancelledError):
        await bus._receive_loop()
    assert received == [event]
