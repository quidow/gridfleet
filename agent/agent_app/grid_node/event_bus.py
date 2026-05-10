from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import zmq
import zmq.asyncio

EventHandler = Callable[[dict[str, Any]], None]


def encode_event_frames(event: dict[str, Any]) -> list[bytes]:
    return [
        str(event["type"]).lower().encode("utf-8"),
        b'""',
        str(uuid4()).encode("ascii"),
        json.dumps(event, sort_keys=True).encode("utf-8"),
    ]


def decode_event_frames(frames: list[bytes]) -> dict[str, Any]:
    for frame in reversed(frames):
        try:
            payload = json.loads(frame.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("type"), str):
            return payload
    raise ValueError("event frames did not contain a JSON event envelope")


class EventBus:
    def __init__(self, *, publish_url: str, subscribe_url: str, heartbeat_sec: float) -> None:
        self._publish_url = publish_url
        self._subscribe_url = subscribe_url
        self._heartbeat_sec = heartbeat_sec
        self._context = zmq.asyncio.Context.instance()
        self._publish_socket: zmq.asyncio.Socket | None = None
        self._subscribe_socket: zmq.asyncio.Socket | None = None
        self._subscriber_task: asyncio.Task[None] | None = None
        self._handlers: list[EventHandler] = []
        self.last_publish_ok_at: float | None = None

    def on_event(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def start(self) -> None:
        self._publish_socket = self._context.socket(zmq.PUB)
        self._subscribe_socket = self._context.socket(zmq.SUB)
        self._subscribe_socket.setsockopt(zmq.SUBSCRIBE, b"")
        if self._publish_url.startswith("inproc://") and self._publish_url == self._subscribe_url:
            self._publish_socket.bind(self._publish_url)
            self._subscribe_socket.connect(self._subscribe_url)
        else:
            self._publish_socket.connect(self._publish_url)
            self._subscribe_socket.connect(self._subscribe_url)
        self._subscriber_task = asyncio.create_task(self._receive_loop())
        await asyncio.sleep(0)

    async def stop(self) -> None:
        if self._subscriber_task is not None:
            self._subscriber_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._subscriber_task
            self._subscriber_task = None
        if self._publish_socket is not None:
            self._publish_socket.close(linger=0)
            self._publish_socket = None
        if self._subscribe_socket is not None:
            self._subscribe_socket.close(linger=0)
            self._subscribe_socket = None

    async def publish(self, event: dict[str, Any]) -> None:
        await self._send_multipart(encode_event_frames(event))
        self.last_publish_ok_at = time.monotonic()

    async def _send_multipart(self, frames: list[bytes]) -> None:
        if self._publish_socket is None:
            raise RuntimeError("event bus is not started")
        await self._publish_socket.send_multipart(frames)

    async def _receive_loop(self) -> None:
        if self._subscribe_socket is None:
            return
        while True:
            frames = await self._subscribe_socket.recv_multipart()
            event = decode_event_frames(list(frames))
            for handler in self._handlers:
                handler(event)
