from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import httpx
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute

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


def write_http_record(
    transcript_path: Path,
    *,
    ts: float,
    kind: Literal["request", "response", "ws_frame"],
    direction: Literal["hub_to_node", "node_to_hub"],
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
) -> None:
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "body_b64": base64.b64encode(body).decode("ascii"),
        "direction": direction,
        "headers": dict(sorted(headers.items())),
        "kind": kind,
        "method": method,
        "path": path,
        "ts": ts,
    }
    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def build_app(*, upstream: str, out: Path) -> Starlette:
    client = httpx.AsyncClient(timeout=None)
    transcript = out / "http.transcript"

    async def proxy_http(request: Request) -> Response:
        body = await request.body()
        write_http_record(
            transcript,
            ts=time.time(),
            kind="request",
            direction="hub_to_node",
            method=request.method,
            path=request.url.path,
            headers=strip_hop_headers(dict(request.headers)),
            body=body,
        )
        upstream_response = await client.request(
            request.method,
            f"{upstream}{request.url.path}",
            params=request.query_params,
            content=body,
            headers=strip_hop_headers(dict(request.headers)),
        )
        response_body = upstream_response.content
        write_http_record(
            transcript,
            ts=time.time(),
            kind="response",
            direction="node_to_hub",
            method=request.method,
            path=request.url.path,
            headers=strip_hop_headers(dict(upstream_response.headers)),
            body=response_body,
        )
        return StreamingResponse(
            iter([response_body]),
            status_code=upstream_response.status_code,
            headers=strip_hop_headers(dict(upstream_response.headers)),
        )

    async def proxy_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        upstream_ws = upstream.replace("http://", "ws://").replace("https://", "wss://")
        async with websockets.connect(f"{upstream_ws}{websocket.url.path}") as remote:

            async def client_to_remote() -> None:
                while True:
                    message = await websocket.receive_text()
                    write_http_record(
                        transcript,
                        ts=time.time(),
                        kind="ws_frame",
                        direction="hub_to_node",
                        method="WEBSOCKET",
                        path=websocket.url.path,
                        headers={},
                        body=message.encode("utf-8"),
                    )
                    await remote.send(message)

            async def remote_to_client() -> None:
                async for message in remote:
                    data = message if isinstance(message, bytes) else message.encode("utf-8")
                    write_http_record(
                        transcript,
                        ts=time.time(),
                        kind="ws_frame",
                        direction="node_to_hub",
                        method="WEBSOCKET",
                        path=websocket.url.path,
                        headers={},
                        body=data,
                    )
                    await websocket.send_bytes(data)

            await asyncio.gather(client_to_remote(), remote_to_client())

    return Starlette(
        routes=[
            Route("/{path:path}", proxy_http, methods=["GET", "POST", "PUT", "DELETE"]),
            WebSocketRoute("/{path:path}", proxy_ws),
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    host, port_text = args.listen.rsplit(":", 1)
    uvicorn.run(build_app(upstream=args.upstream, out=args.out), host=host, port=int(port_text))


if __name__ == "__main__":
    main()
