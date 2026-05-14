from __future__ import annotations

import httpx
import pytest

from agent_app.config import agent_settings
from agent_app.lifespan import HttpPackStateClient


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"packs": []}, request=request)
        return httpx.Response(204, request=request)


@pytest.mark.asyncio
async def test_pack_state_client_sends_manager_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_settings.manager, "manager_auth_username", "machine")
    monkeypatch.setattr(agent_settings.manager, "manager_auth_password", "machine-secret")
    transport = RecordingTransport()
    original_async_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return original_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    client = HttpPackStateClient("http://manager.local", "00000000-0000-0000-0000-000000000001")
    assert await client.fetch_desired() == {"packs": []}
    await client.post_status({"host_id": "00000000-0000-0000-0000-000000000001"})

    assert len(transport.requests) == 2
    for request in transport.requests:
        assert request.headers["authorization"].startswith("Basic ")
