"""End-to-end: real Selenium hub bus → subscriber → doorbell.

Requires the ``selenium-hub`` docker compose service running on the
default ports. Skipped by default; run with ``pytest -m grid``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import zmq
import zmq.asyncio

from app.grid import event_bus_loop, grid_settings
from app.grid.services_container import GridServices
from app.sessions.service_sync import SessionSyncService
from tests.fakes import FakeSettingsReader, make_fake_grid
from tests.helpers import test_event_bus as event_bus

pytestmark = [pytest.mark.grid, pytest.mark.asyncio]

HUB_HOST = "localhost"
# Selenium Grid 4 defaults (per GridConfig docstring):
#   4442 — hub XPUB. Subscribers (SUB sockets) connect here to READ events.
#   4443 — hub XSUB. Publishers (PUB sockets) connect here to WRITE events.
HUB_XPUB_PORT = 4442  # subscribers READ from here
HUB_XSUB_PORT = 4443  # publishers WRITE to here


def _hub_reachable() -> bool:
    try:
        with socket.create_connection((HUB_HOST, HUB_XPUB_PORT), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture(autouse=True)
def _skip_if_no_hub() -> None:
    if not _hub_reachable():
        pytest.skip(f"selenium hub not reachable at {HUB_HOST}:{HUB_XPUB_PORT}")


def _frames(event_type: str, payload: object) -> list[bytes]:
    return [
        event_type.encode("utf-8"),
        b'""',
        str(uuid4()).encode("ascii"),
        json.dumps(payload, sort_keys=True).encode("utf-8"),
    ]


def _fake_session_factory() -> object:
    class _FakeSession:
        async def __aenter__(self) -> object:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    return _FakeSession()


async def test_real_hub_session_created_wakes_session_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    # Point subscriber at the real hub XPUB (subscribers READ from here).
    monkeypatch.setattr(grid_settings, "event_bus_subscribe_url", f"tcp://{HUB_HOST}:{HUB_XPUB_PORT}")

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
        await asyncio.sleep(0.5)  # let SUB connect through the hub proxy
        # Publish a synthetic session-created via the hub's XSUB ingress
        # (publishers WRITE to XSUB; the hub proxies it out via XPUB).
        ctx = zmq.asyncio.Context.instance()
        pub = ctx.socket(zmq.PUB)
        pub.connect(f"tcp://{HUB_HOST}:{HUB_XSUB_PORT}")
        try:
            await asyncio.sleep(0.2)  # PUB → XSUB connect time
            await pub.send_multipart(_frames("session-created", {"id": f"e2e-{uuid4()}"}))
            doorbell = waker._get_doorbell()
            await asyncio.wait_for(doorbell.wait(), timeout=2.0)
        finally:
            pub.close(linger=0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
