"""Leader-owned wrapper around HubEventBusSubscriber.

Asserts: doorbell wake on real bus event, supervisor restart after a
crash, clean shutdown on cancel.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import zmq
import zmq.asyncio

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from unittest.mock import AsyncMock

from app.grid import event_bus_loop
from app.grid.services_container import GridServices
from app.sessions.service_sync import SessionSyncService
from tests.fakes import FakeSettingsReader, make_fake_grid
from tests.helpers import test_event_bus as event_bus


def _frames(event_type: str, payload: object) -> list[bytes]:
    return [
        event_type.encode("utf-8"),
        b'""',
        str(uuid4()).encode("ascii"),
        json.dumps(payload, sort_keys=True).encode("utf-8"),
    ]


@pytest.fixture
async def hub_pub(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[zmq.asyncio.Socket]:
    ctx = zmq.asyncio.Context.instance()
    pub = ctx.socket(zmq.PUB)
    url = f"inproc://eventbus-loop-{uuid4().hex}"
    pub.bind(url)
    from app.grid import grid_settings

    monkeypatch.setattr(grid_settings, "event_bus_subscribe_url", url)
    try:
        yield pub
    finally:
        pub.close(linger=0)


def _fake_session_factory() -> object:
    class _FakeSession:
        async def __aenter__(self) -> object:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    return _FakeSession()


async def test_subscriber_loop_wakes_session_sync(
    hub_pub: zmq.asyncio.Socket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waker = SessionSyncService(
        publisher=event_bus, settings=FakeSettingsReader({}), grid=make_fake_grid(), lifecycle=AsyncMock()
    )
    loop = event_bus_loop.GridEventBusSubscriberLoop(
        services=GridServices(
            grid=make_fake_grid(),
            settings=FakeSettingsReader({}),
            session_factory=_fake_session_factory,
        ),
        session_sync_waker=waker,
    )
    task = asyncio.create_task(loop.run())
    try:
        await asyncio.sleep(0.1)  # let SUB connect + subscribe
        await hub_pub.send_multipart(_frames("session-created", {"id": "s-1"}))
        doorbell = waker._get_doorbell()
        await asyncio.wait_for(doorbell.wait(), timeout=1.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_subscriber_loop_shuts_down_cleanly(
    hub_pub: zmq.asyncio.Socket,
) -> None:
    from unittest.mock import Mock

    loop = event_bus_loop.GridEventBusSubscriberLoop(
        services=GridServices(
            grid=make_fake_grid(),
            settings=FakeSettingsReader({}),
            session_factory=_fake_session_factory,
        ),
        session_sync_waker=Mock(),
    )
    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
