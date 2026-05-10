from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import httpx
import pytest
import websockets
from httpx import Response as HttpxResponse
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_app.grid_node.proxy import proxy_request, proxy_websocket, strip_hop_headers

if TYPE_CHECKING:
    from starlette.websockets import WebSocket


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
async def test_proxy_request_preserves_duplicate_response_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def send(_request: httpx.Request, *, stream: bool) -> httpx.Response:
        return httpx.Response(
            200,
            headers=[("set-cookie", "a=1; Path=/"), ("set-cookie", "b=2; Path=/")],
            content=b"{}",
        )

    async with httpx.AsyncClient() as client:
        monkeypatch.setattr(client, "send", send)
        response = await proxy_request(
            _request("GET", "/status"),
            upstream="http://appium",
            timeout=1.0,
            client=client,
        )

    set_cookie_headers = [
        value.decode("latin-1") for key, value in response.raw_headers if key.lower() == b"set-cookie"
    ]
    assert set_cookie_headers == ["a=1; Path=/", "b=2; Path=/"]


@pytest.mark.asyncio
async def test_proxy_request_closes_upstream_response_when_body_read_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = False

    class FailingReadResponse(HttpxResponse):
        async def aread(self) -> bytes:
            raise RuntimeError("read failed")

        async def aclose(self) -> None:
            nonlocal closed
            closed = True
            await super().aclose()

    async def send(_request: httpx.Request, *, stream: bool) -> httpx.Response:
        return FailingReadResponse(200, content=b"{}")

    async with httpx.AsyncClient() as client:
        monkeypatch.setattr(client, "send", send)
        with pytest.raises(RuntimeError, match="read failed"):
            await proxy_request(
                _request("GET", "/status"),
                upstream="http://appium",
                timeout=1.0,
                client=client,
            )

    assert closed is True


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


@pytest.mark.asyncio
async def test_proxy_websocket_pipes_frames() -> None:
    async def echo(websocket: websockets.ServerConnection) -> None:
        async for message in websocket:
            await websocket.send(message)

    server = await websockets.serve(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def proxy_endpoint(websocket: WebSocket) -> None:
        await proxy_websocket(websocket, upstream=f"http://127.0.0.1:{port}")

    app = Starlette(routes=[WebSocketRoute("/session/{session_id}/se/cdp", proxy_endpoint)])

    def run_client() -> None:
        with TestClient(app).websocket_connect("/session/session-1/se/cdp") as websocket:
            websocket.send_text("ping")
            assert websocket.receive_text() == "ping"

    try:
        await asyncio.to_thread(run_client)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_proxy_websocket_preserves_query_string() -> None:
    seen_paths: list[str] = []

    async def echo(websocket: websockets.ServerConnection) -> None:
        seen_paths.append(websocket.request.path)
        async for message in websocket:
            await websocket.send(message)

    server = await websockets.serve(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def proxy_endpoint(websocket: WebSocket) -> None:
        await proxy_websocket(websocket, upstream=f"ws://127.0.0.1:{port}")

    app = Starlette(routes=[WebSocketRoute("/session/{session_id}/se/cdp", proxy_endpoint)])

    def run_client() -> None:
        with TestClient(app).websocket_connect("/session/session-1/se/cdp?channel=devtools") as websocket:
            websocket.send_text("ping")
            assert websocket.receive_text() == "ping"

    try:
        await asyncio.to_thread(run_client)
    finally:
        server.close()
        await server.wait_closed()

    assert seen_paths == ["/session/session-1/se/cdp?channel=devtools"]


@pytest.mark.asyncio
async def test_proxy_websocket_rejects_client_when_upstream_connect_fails() -> None:
    async def proxy_endpoint(websocket: WebSocket) -> None:
        await proxy_websocket(websocket, upstream="ws://127.0.0.1:1")

    app = Starlette(routes=[WebSocketRoute("/session/{session_id}/se/cdp", proxy_endpoint)])

    def run_client() -> None:
        with pytest.raises(WebSocketDisconnect), TestClient(app).websocket_connect("/session/session-1/se/cdp"):
            pass

    await asyncio.to_thread(run_client)


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
