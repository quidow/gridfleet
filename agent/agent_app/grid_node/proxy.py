from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import httpx
import websockets
from starlette.responses import Response

if TYPE_CHECKING:
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
        headers=strip_hop_headers(dict(request.headers)),
        timeout=timeout,
    )
    try:
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.ConnectError:
        return Response(status_code=502)
    except httpx.TimeoutException:
        return Response(status_code=504)
    response_body = await upstream_response.aread()
    await upstream_response.aclose()
    return Response(
        response_body,
        status_code=upstream_response.status_code,
        headers=strip_hop_headers(dict(upstream_response.headers)),
    )


async def proxy_websocket(websocket: WebSocket, *, upstream: str) -> None:
    await websocket.accept()
    target = f"{upstream}{websocket.url.path}"
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
            task.result()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
