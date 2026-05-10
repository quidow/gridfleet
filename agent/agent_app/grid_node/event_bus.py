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

from agent_app._supervision import ExponentialBackoff

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
        self._publish_backoff = ExponentialBackoff(base=0.1, factor=2.0, cap=5.0, max_attempts=5, window_sec=60.0)
        self.last_publish_ok_at: float | None = None
        self.last_publish_failed_at: float | None = None
        self.publish_failures = 0

    @property
    def publish_socket(self) -> zmq.asyncio.Socket | None:
        return self._publish_socket

    def on_event(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def start(self) -> None:
        self._open_publish_socket()
        self._subscribe_socket = self._context.socket(zmq.SUB)
        self._subscribe_socket.setsockopt(zmq.SUBSCRIBE, b"")
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
        try:
            await self._send_multipart(encode_event_frames(event))
        except Exception:
            now = time.monotonic()
            self.last_publish_failed_at = now
            self.publish_failures += 1
            self._publish_backoff.record_attempt(now)
            self._publish_backoff.next_delay()
            self._recreate_publish_socket()
            raise
        else:
            self.last_publish_ok_at = time.monotonic()
            self._publish_backoff.reset()

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

    def _open_publish_socket(self) -> None:
        self._publish_socket = self._context.socket(zmq.PUB)
        if self._publish_url.startswith("inproc://") and self._publish_url == self._subscribe_url:
            self._publish_socket.bind(self._publish_url)
        else:
            self._publish_socket.connect(self._publish_url)

    def _recreate_publish_socket(self) -> None:
        if self._publish_socket is not None:
            self._publish_socket.close(linger=0)
        self._open_publish_socket()
