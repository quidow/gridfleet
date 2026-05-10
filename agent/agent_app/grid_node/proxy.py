from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from starlette.responses import Response
from websockets.exceptions import ConnectionClosed

if TYPE_CHECKING:
    from collections.abc import Iterable

    from starlette.requests import Request
    from starlette.websockets import WebSocket

HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def strip_hop_headers(headers: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in HOP_HEADERS}


def strip_hop_header_items(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(key, value) for key, value in headers if key.lower() not in HOP_HEADERS]


async def proxy_request(
    request: Request,
    *,
    upstream: str,
    timeout: float,
    client: httpx.AsyncClient,
) -> Response:
    body = await request.body()
    target = f"{upstream}{request.url.path}"
    upstream_request = client.build_request(
        request.method,
        target,
        params=request.query_params,
        content=body,
        headers=strip_hop_header_items(
            [(key.decode("latin-1"), value.decode("latin-1")) for key, value in request.headers.raw]
        ),
        timeout=timeout,
    )
    try:
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.ConnectError:
        return Response(status_code=502)
    except httpx.TimeoutException:
        return Response(status_code=504)
    response_body = await upstream_response.aread()
    response_headers = strip_hop_header_items(upstream_response.headers.multi_items())
    await upstream_response.aclose()
    response = Response(response_body, status_code=upstream_response.status_code)
    for key, value in response_headers:
        response.headers.append(key, value)
    return response


async def proxy_websocket(websocket: WebSocket, *, upstream: str) -> None:
    await websocket.accept()
    target = f"{_websocket_upstream(upstream)}{websocket.url.path}"
    if websocket.url.query:
        target = f"{target}?{websocket.url.query}"
    async with websockets.connect(target) as remote:

        async def client_to_remote() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    await remote.close()
                    return
                if "text" in message:
                    await remote.send(message["text"])
                elif "bytes" in message:
                    await remote.send(message["bytes"])

        async def remote_to_client() -> None:
            async for message in remote:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)

        tasks = {asyncio.create_task(client_to_remote()), asyncio.create_task(remote_to_client())}
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(ConnectionClosed):
                task.result()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _websocket_upstream(upstream: str) -> str:
    parsed = urlsplit(upstream)
    if parsed.scheme == "http":
        return urlunsplit(("ws", parsed.netloc, parsed.path.rstrip("/"), "", ""))
    if parsed.scheme == "https":
        return urlunsplit(("wss", parsed.netloc, parsed.path.rstrip("/"), "", ""))
    return upstream.rstrip("/")
