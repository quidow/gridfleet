from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.events import event_bus

if TYPE_CHECKING:
    from httpx import AsyncClient

    from app.hosts.models import Host

pytestmark = pytest.mark.db


@pytest.mark.asyncio
async def test_get_events_returns_page(client: AsyncClient, db_host: Host) -> None:
    await event_bus.publish("host.status_changed", {"host_id": str(db_host.id), "hostname": "h"})
    await event_bus.drain_handlers()
    resp = await client.get(f"/api/hosts/{db_host.id}/events")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert any(event["type"] == "host.status_changed" for event in body["events"])


@pytest.mark.asyncio
async def test_get_events_type_filter(client: AsyncClient, db_host: Host) -> None:
    await event_bus.publish("host.heartbeat_lost", {"host_id": str(db_host.id), "missed_count": 1})
    await event_bus.publish("host.status_changed", {"host_id": str(db_host.id)})
    await event_bus.drain_handlers()
    resp = await client.get(
        f"/api/hosts/{db_host.id}/events",
        params={"types": "host.heartbeat_lost"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert all(event["type"] == "host.heartbeat_lost" for event in body["events"])
