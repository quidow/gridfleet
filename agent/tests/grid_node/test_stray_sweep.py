from __future__ import annotations

from typing import Any

import pytest

from agent_app.grid_node import hub_status_cache
from agent_app.grid_node.stray_sweep import sweep_stray_registrations


class _Bus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def publish(self, event: dict[str, object]) -> None:
        self.events.append(dict(event))


def _hub_entry(node_id: str, uri: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "uri": uri,
        "availability": "DRAINING",
        "slots": [],
        "osInfo": {"arch": "aarch64", "name": "Mac OS X", "version": "15"},
        "maxSessions": 1,
        "sessionTimeout": 300000,
        "heartbeatPeriod": 5000,
        "version": "4.43.0",
    }


@pytest.mark.asyncio
async def test_sweep_removes_only_own_dead_uris(monkeypatch: pytest.MonkeyPatch) -> None:
    hub_status_cache.clear()

    async def fake_get(url: str, *, fresh: bool = False) -> list[dict[str, Any]]:
        return [
            _hub_entry("live-1", "http://myhost:7301"),  # live relay — keep
            _hub_entry("husk-1", "http://myhost:7302"),  # our URI, no live relay — remove
            _hub_entry("other-1", "http://otherhost:7301"),  # different host — never touch
        ]

    monkeypatch.setattr(hub_status_cache, "get_hub_nodes", fake_get)
    bus = _Bus()
    removed = await sweep_stray_registrations(
        hub_status_url="http://hub:4444",
        bus=bus,
        own_uri=lambda uri: uri.startswith("http://myhost:"),
        live_node_ids={"live-1"},
    )
    assert removed == 1
    assert [e["type"] for e in bus.events] == ["node-removed"]
    assert bus.events[0]["data"]["nodeId"] == "husk-1"
    assert bus.events[0]["data"]["externalUri"] == "http://myhost:7302"


@pytest.mark.asyncio
async def test_sweep_is_silent_when_hub_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    hub_status_cache.clear()

    async def fake_get(url: str, *, fresh: bool = False) -> None:
        return None

    monkeypatch.setattr(hub_status_cache, "get_hub_nodes", fake_get)
    bus = _Bus()
    assert (
        await sweep_stray_registrations(
            hub_status_url="http://hub:4444", bus=bus, own_uri=lambda uri: True, live_node_ids=set()
        )
        == 0
    )
    assert bus.events == []


@pytest.mark.asyncio
async def test_sweep_disabled_without_url() -> None:
    bus = _Bus()
    assert (
        await sweep_stray_registrations(hub_status_url="", bus=bus, own_uri=lambda uri: True, live_node_ids=set()) == 0
    )
    assert bus.events == []


@pytest.mark.asyncio
async def test_sweep_survives_publish_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    hub_status_cache.clear()

    async def fake_get(url: str, *, fresh: bool = False) -> list[dict[str, Any]]:
        return [
            _hub_entry("husk-1", "http://myhost:7302"),
            _hub_entry("husk-2", "http://myhost:7303"),
        ]

    monkeypatch.setattr(hub_status_cache, "get_hub_nodes", fake_get)

    class _FailingFirstBus(_Bus):
        async def publish(self, event: dict[str, object]) -> None:
            if not self.events:
                self.events.append({"type": "failed"})
                raise RuntimeError("zmq down")
            self.events.append(dict(event))

    bus = _FailingFirstBus()
    removed = await sweep_stray_registrations(
        hub_status_url="http://hub:4444",
        bus=bus,
        own_uri=lambda uri: uri.startswith("http://myhost:"),
        live_node_ids=set(),
    )
    assert removed == 1  # second husk still swept after the first publish failed
