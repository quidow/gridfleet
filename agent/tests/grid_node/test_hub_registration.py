from __future__ import annotations

from typing import Any

import pytest

from agent_app.grid_node import hub_status_cache
from agent_app.grid_node.hub_registration import HubObserved, observe_hub_node


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
