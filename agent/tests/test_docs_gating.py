"""/docs env gating regression guard."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from agent_app.registration import RegistrationService

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI


@pytest.fixture(autouse=True)
def _restore_default_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield
    monkeypatch.delenv("AGENT_ENVIRONMENT", raising=False)
    import agent_app.config as cfg

    importlib.reload(cfg)
    import agent_app.main as main

    importlib.reload(main)


def _reload_app(monkeypatch: pytest.MonkeyPatch, environment: str) -> FastAPI:
    monkeypatch.setenv("AGENT_ENVIRONMENT", environment)
    import agent_app.config as cfg

    importlib.reload(cfg)
    import agent_app.main as main

    importlib.reload(main)
    return main.app


class _RecordingTransport(httpx.AsyncBaseTransport):
    """Records every request the agent makes and answers nothing useful."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(503, request=request)


@contextlib.contextmanager
def _lifespan_traffic(monkeypatch: pytest.MonkeyPatch) -> Iterator[_RecordingTransport]:
    """Capture everything the lifespan sends, so the test can assert it sent nothing.

    These tests want the app; the lifespan that comes with it registers a host.
    Against a live backend that steals the boot fence from the agent actually
    running on this machine, so ``run`` never gets scheduled here. With it
    stubbed the status/pack/node/probe loops all block on ``host_identity``,
    which is why the recorded traffic is empty rather than merely harmless.
    """
    transport = _RecordingTransport()
    original_async_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return original_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    stop = asyncio.Event()

    async def _wait_forever(*_args: object, **_kwargs: object) -> None:
        await stop.wait()

    with patch.object(RegistrationService, "run", side_effect=_wait_forever):
        try:
            yield transport
        finally:
            stop.set()


def test_docs_visible_when_environment_is_local(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _reload_app(monkeypatch, "local")
    assert app.openapi_url == "/openapi.json"
    with _lifespan_traffic(monkeypatch) as traffic, TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 200
    assert [str(request.url) for request in traffic.requests] == []


def test_docs_hidden_when_environment_is_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _reload_app(monkeypatch, "prod")
    assert app.openapi_url is None
    with _lifespan_traffic(monkeypatch) as traffic, TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
    assert [str(request.url) for request in traffic.requests] == []
