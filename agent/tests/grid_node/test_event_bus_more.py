from __future__ import annotations

import asyncio
import contextlib

import pytest

from agent_app.grid_node.event_bus import EventBus, decode_event_frames
from agent_app.grid_node.protocol import EventType, event_envelope


class FakeSubscribeSocket:
    def __init__(self, frames: list[list[bytes]]) -> None:
        self._frames = frames

    async def recv_multipart(self) -> list[bytes]:
        await asyncio.sleep(0)
        if not self._frames:
            raise asyncio.CancelledError
        return self._frames.pop(0)


def test_decode_event_frames_too_few_frames() -> None:
    with pytest.raises(ValueError, match="expected 4 grid event-bus frames"):
        decode_event_frames([b"type"])


def test_decode_event_frames_empty_type() -> None:
    with pytest.raises(ValueError, match="missing event type"):
        decode_event_frames([b"", b'""', b"id", b"{}"])


def test_decode_event_frames_malformed_json_data() -> None:
    with pytest.raises(ValueError, match="malformed grid event-bus frames"):
        decode_event_frames([b"type", b'""', b"id", b"not-json"])


def test_decode_event_frames_malformed_utf8_type() -> None:
    with pytest.raises(ValueError, match="malformed grid event-bus frames"):
        decode_event_frames([b"\xff\xfe", b'""', b"id", b"{}"])


@pytest.mark.asyncio
async def test_event_bus_start_is_idempotent() -> None:
    bus = EventBus(publish_url="inproc://idempotent", subscribe_url="inproc://idempotent", heartbeat_sec=1.0)
    await bus.start()
    assert bus.publish_socket is not None
    await bus.start()  # no-op
    await bus.stop()


@pytest.mark.asyncio
async def test_event_bus_publish_when_socket_is_none_raises() -> None:
    bus = EventBus(publish_url="inproc://none", subscribe_url="inproc://none", heartbeat_sec=1.0)
    with pytest.raises(RuntimeError, match="event bus is not started"):
        await bus.publish(event_envelope(EventType.NODE_STATUS, {}))


@pytest.mark.asyncio
async def test_event_bus_stop_is_idempotent() -> None:
    bus = EventBus(publish_url="inproc://stop-idempotent", subscribe_url="inproc://stop-idempotent", heartbeat_sec=1.0)
    await bus.start()
    await bus.stop()
    await bus.stop()
    assert bus.publish_socket is None


@pytest.mark.asyncio
async def test_event_bus_backoff_exhausted_raises_without_recreate() -> None:
    bus = EventBus(publish_url="inproc://backoff", subscribe_url="inproc://backoff", heartbeat_sec=0.1)
    await bus.start()

    async def always_fail(_frames: list[bytes]) -> None:
        raise RuntimeError("send boom")

    bus._send_multipart = always_fail  # type: ignore[method-assign]
    # Exhaust 5 attempts quickly
    for _ in range(5):
        with contextlib.suppress(RuntimeError):
            await bus.publish(event_envelope(EventType.NODE_STATUS, {}))
    with pytest.raises(RuntimeError, match="send boom"):
        await bus.publish(event_envelope(EventType.NODE_STATUS, {}))
    await bus.stop()


@pytest.mark.asyncio
async def test_event_bus_receive_loop_warns_on_looks_like_event() -> None:
    frames = [b"not-json-topic", b'""', b"id", b"{bad"]  # looks like event but malformed json
    bus = EventBus(publish_url="inproc://warn", subscribe_url="inproc://warn", heartbeat_sec=1.0)
    bus._subscribe_socket = FakeSubscribeSocket([frames])
    with pytest.raises(asyncio.CancelledError):
        await bus._receive_loop()


@pytest.mark.asyncio
async def test_event_bus_receive_loop_debug_on_single_frame_probe() -> None:
    bus = EventBus(publish_url="inproc://probe", subscribe_url="inproc://probe", heartbeat_sec=1.0)
    bus._subscribe_socket = FakeSubscribeSocket([[b"GET / HTTP/1.1"]])
    with pytest.raises(asyncio.CancelledError):
        await bus._receive_loop()
