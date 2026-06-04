from __future__ import annotations

import httpx
import pytest

from agent_app.grid_node import hub_status_cache


class _FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse | Exception) -> None:
        self.response = response
        self.calls: list[str] = []

    async def get(self, url: str, *, timeout: float | None = None) -> _FakeResponse:
        self.calls.append(url)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    hub_status_cache.clear()


def _install(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    monkeypatch.setattr("agent_app.grid_node.hub_status_cache.http_client.get_client", lambda: client)


@pytest.mark.asyncio
async def test_returns_node_dicts_and_filters_non_dicts(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"value": {"nodes": [{"id": "n1"}, "garbage", {"id": "n2"}]}}
    client = _FakeClient(_FakeResponse(200, payload))
    _install(monkeypatch, client)
    nodes = await hub_status_cache.get_hub_nodes("http://hub:4444/se/grid/node")
    assert nodes == [{"id": "n1"}, {"id": "n2"}]
    assert client.calls == ["http://hub:4444/se/grid/node/status"]


@pytest.mark.asyncio
async def test_second_call_within_ttl_uses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(_FakeResponse(200, {"value": {"nodes": [{"id": "n1"}]}}))
    _install(monkeypatch, client)
    first = await hub_status_cache.get_hub_nodes("http://hub:4444")
    second = await hub_status_cache.get_hub_nodes("http://hub:4444")
    assert first == second == [{"id": "n1"}]
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_non_200_returns_none_and_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(_FakeResponse(503, {}))
    _install(monkeypatch, client)
    assert await hub_status_cache.get_hub_nodes("http://hub:4444") is None
    assert await hub_status_cache.get_hub_nodes("http://hub:4444") is None
    assert len(client.calls) == 1  # outage costs one probe per TTL, not one per node


@pytest.mark.asyncio
async def test_http_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(httpx.ConnectError("refused"))
    _install(monkeypatch, client)
    assert await hub_status_cache.get_hub_nodes("http://hub:4444") is None


@pytest.mark.asyncio
async def test_unparseable_body_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(_FakeResponse(200, ValueError("not json")))
    _install(monkeypatch, client)
    assert await hub_status_cache.get_hub_nodes("http://hub:4444") is None


@pytest.mark.asyncio
async def test_urls_are_cached_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(_FakeResponse(200, {"value": {"nodes": []}}))
    _install(monkeypatch, client)
    await hub_status_cache.get_hub_nodes("http://hub-a:4444")
    await hub_status_cache.get_hub_nodes("http://hub-b:4444")
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_fresh_fetch_bypasses_and_updates_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(_FakeResponse(200, {"value": {"nodes": [{"id": "n1"}]}}))
    _install(monkeypatch, client)
    await hub_status_cache.get_hub_nodes("http://hub:4444")  # populate cache
    client.response = _FakeResponse(200, {"value": {"nodes": [{"id": "n1"}, {"id": "n2"}]}})
    fresh = await hub_status_cache.get_hub_nodes("http://hub:4444", fresh=True)
    assert fresh == [{"id": "n1"}, {"id": "n2"}]
    assert len(client.calls) == 2
    # the fresh result replaced the cached snapshot for subsequent callers
    cached = await hub_status_cache.get_hub_nodes("http://hub:4444")
    assert cached == [{"id": "n1"}, {"id": "n2"}]
    assert len(client.calls) == 2
