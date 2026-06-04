from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_app.grid_node.proxy import proxy_request, proxy_websocket
from agent_app.grid_node.upstream_pool import (
    UpstreamConnectError,
    UpstreamError,
    UpstreamResponse,
    UpstreamTimeoutError,
)

if TYPE_CHECKING:
    from starlette.websockets import WebSocket


class FakePool:
    def __init__(self, *, response: UpstreamResponse | None = None, error: Exception | None = None) -> None:
        self.requests: list[tuple[str, str, list[tuple[str, str]], bytes]] = []
        self._response = response
        self._error = error

    async def request(self, method: str, target: str, headers: list[tuple[str, str]], body: bytes) -> UpstreamResponse:
        self.requests.append((method, target, headers, body))
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


@pytest.mark.asyncio
async def test_proxy_request_forwards_status_and_strips_hop_headers() -> None:
    pool = FakePool(
        response=UpstreamResponse(
            200,
            [(b"connection", b"close"), (b"x-appium", b"stub"), (b"content-type", b"application/json")],
            b'{"value": {"ready": true}}',
        )
    )
    response = await proxy_request(_request("GET", "/status"), pool=pool)

    assert response.status_code == 200
    assert json.loads(response.body)["value"]["ready"] is True
    assert response.headers["x-appium"] == "stub"
    assert "connection" not in response.headers


@pytest.mark.asyncio
async def test_proxy_request_strips_request_hop_headers_and_keeps_query() -> None:
    pool = FakePool(response=UpstreamResponse(200, [], b"{}"))
    request = _request(
        "GET",
        "/session/s1/screenshot",
        query=b"scale=2",
        headers=[(b"connection", b"keep-alive"), (b"x-custom", b"yes")],
    )
    await proxy_request(request, pool=pool)

    method, target, headers, _body = pool.requests[0]
    assert method == "GET"
    assert target == "/session/s1/screenshot?scale=2"
    assert ("x-custom", "yes") in headers
    assert all(name.lower() != "connection" for name, _value in headers)


@pytest.mark.asyncio
async def test_proxy_request_preserves_duplicate_response_headers() -> None:
    pool = FakePool(
        response=UpstreamResponse(200, [(b"set-cookie", b"a=1; Path=/"), (b"set-cookie", b"b=2; Path=/")], b"{}")
    )
    response = await proxy_request(_request("GET", "/status"), pool=pool)

    set_cookie_headers = [
        value.decode("latin-1") for key, value in response.raw_headers if key.lower() == b"set-cookie"
    ]
    assert set_cookie_headers == ["a=1; Path=/", "b=2; Path=/"]


@pytest.mark.asyncio
async def test_proxy_request_emits_single_content_length() -> None:
    # Starlette's Response() sets Content-Length for the buffered body; the
    # upstream copy must not be appended on top. The duplicate was harmless
    # behind the Java hub's client but is rejected as malformed by strict h1
    # parsers (e.g. the relay fast-lane sidecar), surfacing as a 502 on every
    # proxied control-leg response.
    pool = FakePool(
        response=UpstreamResponse(
            200, [(b"content-type", b"application/json"), (b"content-length", b"15")], b'{"value": null}'
        )
    )
    response = await proxy_request(_request("DELETE", "/session/abc"), pool=pool)

    content_length_headers = [value for key, value in response.raw_headers if key.lower() == b"content-length"]
    assert content_length_headers == [b"15"]


@pytest.mark.asyncio
async def test_proxy_request_propagates_mid_response_failure() -> None:
    pool = FakePool(error=UpstreamError("upstream closed mid-response"))
    with pytest.raises(UpstreamError, match="mid-response"):
        await proxy_request(_request("GET", "/status"), pool=pool)


@pytest.mark.asyncio
async def test_proxy_connection_refused_returns_502() -> None:
    pool = FakePool(error=UpstreamConnectError("connect failed"))
    response = await proxy_request(_request("GET", "/status"), pool=pool)
    assert response.status_code == 502


@pytest.mark.asyncio
async def test_proxy_timeout_returns_504() -> None:
    pool = FakePool(error=UpstreamTimeoutError("deadline exceeded"))
    response = await proxy_request(_request("GET", "/status"), pool=pool)
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


def _request(
    method: str,
    path: str,
    *,
    body: bytes = b"",
    query: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query,
            "headers": headers or [],
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 50000),
        },
        receive,
    )
