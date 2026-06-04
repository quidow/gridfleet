"""HubEventBusSubscriber lifecycle + dispatch.

Uses an in-process inproc ZMQ socket so tests do not depend on a real
hub. Asserts: doorbell fires on each known event, malformed frames
increment the failure counter without killing the loop, unknown event
types are dropped, and stop() cleanly shuts the socket down.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import pytest
import zmq
import zmq.asyncio

from app.grid.event_bus import HubEventBusSubscriber


def _frames(event_type: str, payload: object) -> list[bytes]:
    return [
        event_type.encode("utf-8"),
        b'""',
        str(uuid4()).encode("ascii"),
        json.dumps(payload, sort_keys=True).encode("utf-8"),
    ]


@pytest.fixture
async def inproc_pair() -> AsyncIterator[tuple[zmq.asyncio.Socket, str]]:
    """Publish socket bound to inproc + the URL the subscriber should connect to."""
    ctx = zmq.asyncio.Context.instance()
    pub = ctx.socket(zmq.PUB)
    url = f"inproc://eventbus-{uuid4().hex}"
    pub.bind(url)
    try:
        yield pub, url
    finally:
        pub.close(linger=0)


async def _await_event_set(event: asyncio.Event, timeout: float = 1.0) -> None:
    await asyncio.wait_for(event.wait(), timeout=timeout)


async def test_doorbell_fires_on_session_created(inproc_pair: tuple[zmq.asyncio.Socket, str]) -> None:
    pub, url = inproc_pair
    doorbell = asyncio.Event()
    sub = HubEventBusSubscriber(subscribe_url=url, on_event=lambda _e: doorbell.set())
    await sub.start()
    try:
        # Slow-joiner: give SUB time to install the subscription on PUB.
        await asyncio.sleep(0.05)
        await pub.send_multipart(_frames("session-created", {"id": "s-1"}))
        await _await_event_set(doorbell)
    finally:
        await sub.stop()


async def test_doorbell_fires_on_session_closed_legacy_payload(
    inproc_pair: tuple[zmq.asyncio.Socket, str],
) -> None:
    pub, url = inproc_pair
    doorbell = asyncio.Event()
    sub = HubEventBusSubscriber(subscribe_url=url, on_event=lambda _e: doorbell.set())
    await sub.start()
    try:
        await asyncio.sleep(0.05)
        await pub.send_multipart(_frames("session-closed", "s-1"))
        await _await_event_set(doorbell)
    finally:
        await sub.stop()


async def test_doorbell_fires_on_node_added(inproc_pair: tuple[zmq.asyncio.Socket, str]) -> None:
    pub, url = inproc_pair
    doorbell = asyncio.Event()
    sub = HubEventBusSubscriber(subscribe_url=url, on_event=lambda _e: doorbell.set())
    await sub.start()
    try:
        await asyncio.sleep(0.05)
        await pub.send_multipart(_frames("node-added", {"nodeId": "n-1"}))
        await _await_event_set(doorbell)
    finally:
        await sub.stop()


async def test_doorbell_fires_on_node_removed(inproc_pair: tuple[zmq.asyncio.Socket, str]) -> None:
    pub, url = inproc_pair
    doorbell = asyncio.Event()
    sub = HubEventBusSubscriber(subscribe_url=url, on_event=lambda _e: doorbell.set())
    await sub.start()
    try:
        await asyncio.sleep(0.05)
        await pub.send_multipart(_frames("node-removed", {"nodeId": "n-1"}))
        await _await_event_set(doorbell)
    finally:
        await sub.stop()


async def test_unknown_event_type_does_not_fire_doorbell(
    inproc_pair: tuple[zmq.asyncio.Socket, str],
) -> None:
    pub, url = inproc_pair
    doorbell = asyncio.Event()
    sub = HubEventBusSubscriber(subscribe_url=url, on_event=lambda _e: doorbell.set())
    await sub.start()
    try:
        await asyncio.sleep(0.05)
        await pub.send_multipart(_frames("node-heartbeat", {"id": "node-1"}))
        # Doorbell must not fire — node-heartbeat is observed but not
        # actionable for session_sync. Give a brief window to confirm.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(doorbell.wait(), timeout=0.1)
        assert sub.metrics.events_received["node-heartbeat"] == 1
    finally:
        await sub.stop()


async def test_malformed_frames_increment_failure_counter(
    inproc_pair: tuple[zmq.asyncio.Socket, str],
) -> None:
    pub, url = inproc_pair
    doorbell = asyncio.Event()
    sub = HubEventBusSubscriber(subscribe_url=url, on_event=lambda _e: doorbell.set())
    await sub.start()
    try:
        await asyncio.sleep(0.05)
        # Three frames instead of four.
        await pub.send_multipart([b"session-created", b'""', b"id"])
        await asyncio.sleep(0.1)
        assert sub.metrics.decode_failures == 1
        assert not doorbell.is_set()
    finally:
        await sub.stop()


async def test_stop_is_idempotent(inproc_pair: tuple[zmq.asyncio.Socket, str]) -> None:
    _pub, url = inproc_pair
    sub = HubEventBusSubscriber(subscribe_url=url, on_event=lambda _e: None)
    await sub.start()
    await sub.stop()
    await sub.stop()  # second stop must be a no-op, not raise
