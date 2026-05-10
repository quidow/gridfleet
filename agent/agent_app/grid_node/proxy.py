from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.responses import Response

if TYPE_CHECKING:
    import httpx
    from starlette.requests import Request

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
    upstream_response = await client.send(upstream_request, stream=True)
    response_body = await upstream_response.aread()
    await upstream_response.aclose()
    return Response(
        response_body,
        status_code=upstream_response.status_code,
        headers=strip_hop_headers(dict(upstream_response.headers)),
    )
