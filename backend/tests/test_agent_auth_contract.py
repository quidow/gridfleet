from __future__ import annotations

import base64

import httpx
import pytest

from app.services import agent_operations


def _make_capturing_factory() -> tuple[type, list[str | None]]:
    captured: list[str | None] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(_handler)

    class _Factory:
        def __init__(self, **_kwargs: object) -> None:
            self._client = httpx.AsyncClient(transport=transport, base_url="http://agent.local")

        async def __aenter__(self) -> httpx.AsyncClient:
            return self._client

        async def __aexit__(self, *_exc: object) -> bool:
            await self._client.aclose()
            return False

    return _Factory, captured


@pytest.mark.asyncio
async def test_backend_sends_basic_auth_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent_operations,
        "_agent_basic_auth",
        lambda: httpx.BasicAuth("ops", "secret"),
    )
    factory, captured = _make_capturing_factory()

    payload = await agent_operations.agent_health(
        "agent.local",
        agent_port=5100,
        http_client_factory=factory,
    )

    assert payload == {"status": "ok"}
    expected = "Basic " + base64.b64encode(b"ops:secret").decode("ascii")
    assert captured == [expected]


@pytest.mark.asyncio
async def test_backend_omits_authorization_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_operations, "_agent_basic_auth", lambda: None)
    factory, captured = _make_capturing_factory()

    payload = await agent_operations.agent_health(
        "agent.local",
        agent_port=5100,
        http_client_factory=factory,
    )

    assert payload == {"status": "ok"}
    assert captured == [None]
