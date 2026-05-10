from __future__ import annotations

import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_app.grid_node.proxy import proxy_request, strip_hop_headers


def test_proxy_strips_hop_by_hop_headers() -> None:
    headers = {
        "connection": "keep-alive",
        "content-type": "application/json",
        "transfer-encoding": "chunked",
    }
    assert strip_hop_headers(headers) == {"content-type": "application/json"}


@pytest.mark.asyncio
async def test_proxy_request_forwards_status() -> None:
    async def status(_request: Request) -> JSONResponse:
        return JSONResponse({"value": {"ready": True}}, headers={"connection": "close", "x-appium": "stub"})

    app = Starlette(routes=[Route("/status", status)])
    transport = httpx.ASGITransport(app=app)
    request = _request("GET", "/status")
    async with httpx.AsyncClient(transport=transport, base_url="http://appium") as client:
        response = await proxy_request(request, upstream="http://appium", timeout=1.0, client=client)

    assert response.status_code == 200
    assert json.loads(response.body)["value"]["ready"] is True
    assert response.headers["x-appium"] == "stub"
    assert "connection" not in response.headers


@pytest.mark.asyncio
async def test_proxy_connection_refused_returns_502() -> None:
    async with httpx.AsyncClient() as client:
        response = await proxy_request(
            _request("GET", "/status"),
            upstream="http://127.0.0.1:1",
            timeout=0.1,
            client=client,
        )
    assert response.status_code == 502


@pytest.mark.asyncio
async def test_proxy_timeout_returns_504(monkeypatch: pytest.MonkeyPatch) -> None:
    async def raise_timeout(*_args: object, **_kwargs: object) -> object:
        raise httpx.TimeoutException("timed out")

    async with httpx.AsyncClient() as client:
        monkeypatch.setattr(client, "send", raise_timeout)
        response = await proxy_request(
            _request("GET", "/status"),
            upstream="http://127.0.0.1:5555",
            timeout=0.1,
            client=client,
        )
    assert response.status_code == 504


def _request(method: str, path: str, *, body: bytes = b"") -> Request:
    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 50000),
        },
        receive,
    )
