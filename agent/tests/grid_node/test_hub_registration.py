from __future__ import annotations

from typing import Any

import pytest

from agent_app.grid_node import hub_status_cache
from agent_app.grid_node.hub_registration import HubObserved, HubRegistrationReconciler, observe_hub_node


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    hub_status_cache.clear()


def _hub_node(node_id: str, *, availability: str = "UP", run_id: str | None = "free") -> dict[str, Any]:
    stereotype: dict[str, Any] = {"platformName": "Android"}
    if run_id is not None:
        stereotype["gridfleet:run_id"] = run_id
    return {
        "id": node_id,
        "availability": availability,
        "slots": [{"id": {"hostId": node_id, "id": "slot-1"}, "stereotype": stereotype, "session": None}],
    }


@pytest.mark.asyncio
async def test_observe_present_up(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(url: str, *, fresh: bool = False) -> list[dict[str, Any]]:
        return [_hub_node("n1", availability="UP", run_id="free")]

    monkeypatch.setattr(hub_status_cache, "get_hub_nodes", fake_get)
    observed = await observe_hub_node("http://hub:4444", "n1")
    assert observed == HubObserved(present=True, availability="UP", run_id="free")


@pytest.mark.asyncio
async def test_observe_draining_with_stale_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(url: str, *, fresh: bool = False) -> list[dict[str, Any]]:
        return [_hub_node("n1", availability="DRAINING", run_id="18f43b3e")]

    monkeypatch.setattr(hub_status_cache, "get_hub_nodes", fake_get)
    observed = await observe_hub_node("http://hub:4444", "n1")
    assert observed == HubObserved(present=True, availability="DRAINING", run_id="18f43b3e")


@pytest.mark.asyncio
async def test_observe_absent_confirms_with_fresh_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    async def fake_get(url: str, *, fresh: bool = False) -> list[dict[str, Any]]:
        calls.append(fresh)
        return []

    monkeypatch.setattr(hub_status_cache, "get_hub_nodes", fake_get)
    observed = await observe_hub_node("http://hub:4444", "n1")
    assert observed == HubObserved(present=False, availability=None, run_id=None)
    assert calls == [False, True]  # absence is only definitive on a cache-bypassing fetch


@pytest.mark.asyncio
async def test_observe_unreachable_hub_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(url: str, *, fresh: bool = False) -> None:
        return None

    monkeypatch.setattr(hub_status_cache, "get_hub_nodes", fake_get)
    assert await observe_hub_node("http://hub:4444", "n1") is None


@pytest.mark.asyncio
async def test_observe_disabled_when_no_url() -> None:
    assert await observe_hub_node("", "n1") is None


class _Bus:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def publish(self, event: dict[str, object]) -> None:
        self.events.append(str(event["type"]))


def _reconciler(
    bus: _Bus,
    observations: list[HubObserved | None],
    *,
    local_run_id: str = "free",
    busy: bool = False,
) -> HubRegistrationReconciler:
    queue = list(observations)

    async def observe(fresh: bool) -> HubObserved | None:
        return queue.pop(0) if queue else (observations[-1] if observations else None)

    return HubRegistrationReconciler(
        node_id="n1",
        bus=bus,
        node_payload=lambda: {"nodeId": "n1"},
        local_run_id=lambda: local_run_id,
        observe=observe,
        has_busy_slots=lambda: busy,
        drain_grace_sec=lambda: 0.2,
    )


UP_FREE = HubObserved(present=True, availability="UP", run_id="free")
DRAINING_STALE = HubObserved(present=True, availability="DRAINING", run_id="old-run")
ABSENT = HubObserved(present=False, availability=None, run_id=None)


@pytest.mark.asyncio
async def test_registered_and_absent_publishes_added() -> None:
    bus = _Bus()
    rec = _reconciler(bus, [ABSENT])
    rec.set_desired("registered")
    await rec.converge()
    assert bus.events == ["node-added", "node-heartbeat"]  # NODE_STATUS's wire type is "node-heartbeat"


@pytest.mark.asyncio
async def test_registered_and_up_matching_is_noop() -> None:
    bus = _Bus()
    rec = _reconciler(bus, [UP_FREE])
    rec.set_desired("registered")
    await rec.converge()
    assert bus.events == []
    assert rec.is_registered_with_hub() is True


@pytest.mark.asyncio
async def test_registered_and_draining_husk_removes_confirms_readds() -> None:
    """The F-G2 wedge cell: hub holds a DRAINING husk with a stale run_id
    under our node-id — converge must clear it and land the fresh
    registration, with absence confirmed before re-adding."""
    bus = _Bus()
    rec = _reconciler(bus, [DRAINING_STALE, ABSENT])  # second observe = post-REMOVED confirm
    rec.set_desired("registered")
    await rec.converge()
    # No drain/drain-complete: the hub already considers a DRAINING husk drained.
    assert bus.events == ["node-removed", "node-added", "node-heartbeat"]


@pytest.mark.asyncio
async def test_registered_with_stale_run_id_rotates() -> None:
    bus = _Bus()
    stale_up = HubObserved(present=True, availability="UP", run_id="old-run")
    rec = _reconciler(bus, [stale_up, ABSENT])
    rec.set_desired("registered")
    await rec.converge()
    # Caps rotation: drain first so in-flight sessions finish, then remove/re-add —
    # same proven sequence the old reregister_with_caps_update published.
    assert bus.events == [
        "node-drain-started",
        "node-drain-complete",
        "node-removed",
        "node-added",
        "node-heartbeat",
    ]


@pytest.mark.asyncio
async def test_draining_and_up_publishes_drain_only() -> None:
    bus = _Bus()
    rec = _reconciler(bus, [UP_FREE])
    rec.set_desired("draining")
    await rec.converge()
    assert bus.events == ["node-drain-started"]  # never DRAIN_COMPLETE, never a stop


@pytest.mark.asyncio
async def test_draining_and_absent_is_noop() -> None:
    bus = _Bus()
    rec = _reconciler(bus, [ABSENT])
    rec.set_desired("draining")
    await rec.converge()
    assert bus.events == []


@pytest.mark.asyncio
async def test_unknown_hub_never_churns() -> None:
    bus = _Bus()
    rec = _reconciler(bus, [None])
    rec.set_desired("registered")
    rec.mark_announced()  # past startup
    await rec.converge()
    assert bus.events == []


@pytest.mark.asyncio
async def test_first_converge_announces_blindly_when_hub_unknown() -> None:
    """Startup parity with the old blind NODE_ADDED: before the first
    successful announce, an unknown hub must not leave the node unannounced."""
    bus = _Bus()
    rec = _reconciler(bus, [None])
    rec.set_desired("registered")
    await rec.converge()
    assert bus.events == ["node-added", "node-heartbeat"]


@pytest.mark.asyncio
async def test_absent_desired_removes_and_confirms() -> None:
    bus = _Bus()
    rec = _reconciler(bus, [ABSENT])  # post-REMOVED confirm says gone
    rec.set_desired("absent")
    await rec.converge()
    # Parity with the old stop(): REMOVED only — now confirmed via probe.
    assert bus.events == ["node-removed"]


@pytest.mark.asyncio
async def test_absent_desired_retries_removed_until_hub_drops_it() -> None:
    bus = _Bus()
    rec = _reconciler(bus, [UP_FREE, UP_FREE, ABSENT])  # two failed confirms, then gone
    rec.set_desired("absent")
    await rec.converge()
    assert bus.events.count("node-removed") == 3


@pytest.mark.asyncio
async def test_converge_serialized_heartbeat_pass_skips_while_locked() -> None:
    bus = _Bus()
    rec = _reconciler(bus, [UP_FREE])
    rec.set_desired("registered")
    async with rec._lock:  # simulate an in-flight converge
        await rec.try_converge()  # heartbeat path must not block or double-publish
    assert bus.events == []
