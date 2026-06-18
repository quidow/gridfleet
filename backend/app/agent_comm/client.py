from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import TYPE_CHECKING, Protocol, Self, cast

import httpx2 as httpx
from httpx2._types import HeaderTypes, QueryParamTypes

from app.core.errors import AgentUnreachableError, CircuitOpenError, classify_httpx_transport
from app.core.metrics_recorders import record_agent_call
from app.core.observability import REQUEST_ID_HEADER, get_request_id

if TYPE_CHECKING:
    from app.agent_comm.protocols import CircuitBreakerProtocol

type QueryParams = QueryParamTypes | None
type JsonBody = object | None
type RequestHeaders = HeaderTypes | None

# Methods safe to retry once on a stale-keepalive disconnect: the server closed
# the pooled connection before reading the request, so re-sending cannot cause a
# duplicate side effect.
_IDEMPOTENT_METHODS = frozenset({"get", "head"})


class AgentHttpClient(Protocol):
    async def __aenter__(self) -> Self:
        raise NotImplementedError

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> bool:
        raise NotImplementedError

    async def get(
        self,
        url: str,
        *,
        params: QueryParams = None,
        headers: RequestHeaders = None,
        timeout: float | int | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.Response:
        raise NotImplementedError

    async def post(
        self,
        url: str,
        *,
        params: QueryParams = None,
        headers: RequestHeaders = None,
        json: JsonBody = None,
        timeout: float | int | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.Response:
        raise NotImplementedError


type AgentClientFactory = Callable[..., AgentHttpClient] | type[httpx.AsyncClient]


def _request_kwargs(
    method: str,
    *,
    headers: dict[str, str],
    params: QueryParams,
    timeout: float | int | None,
    auth: httpx.Auth | None = None,
    json_body: JsonBody,
) -> dict[str, object]:
    kwargs: dict[str, object] = {"headers": headers}
    if params is not None:
        kwargs["params"] = params
    if timeout is not None:
        kwargs["timeout"] = timeout
    if auth is not None:
        kwargs["auth"] = auth
    if json_body is not None and method not in {"get", "head"}:
        kwargs["json"] = json_body
    return kwargs


def build_agent_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    merged = dict(headers or {})
    request_id = get_request_id()
    if request_id:
        merged.setdefault(REQUEST_ID_HEADER, request_id)
    return merged


async def request(
    method: str,
    url: str,
    *,
    endpoint: str,
    host: str,
    client_mode: str = "fresh",
    client: AgentHttpClient | None = None,
    client_factory: AgentClientFactory = httpx.AsyncClient,
    headers: dict[str, str] | None = None,
    params: QueryParams = None,
    json_body: JsonBody = None,
    timeout: float | int | None = None,
    auth: httpx.Auth | None = None,
    circuit_breaker: CircuitBreakerProtocol,
) -> httpx.Response:
    request_headers = build_agent_headers(headers)
    request_kwargs = _request_kwargs(
        method.lower(),
        headers=request_headers,
        params=params,
        timeout=timeout,
        auth=auth,
        json_body=json_body,
    )
    started = perf_counter()
    outcome = "success"
    retry_after = await circuit_breaker.before_request(host)
    if retry_after is not None:
        record_agent_call(
            host=host,
            endpoint=endpoint,
            outcome="circuit_open",
            client_mode="skipped_circuit_open",
            duration_seconds=0.0,
        )
        raise CircuitOpenError(host, retry_after_seconds=retry_after)
    try:
        response: httpx.Response
        method_name = method.lower()
        if client is None:
            async with client_factory() as owned_client:
                requester = getattr(owned_client, method_name)
                response = cast("httpx.Response", await requester(url, **request_kwargs))
        else:
            requester = getattr(client, method_name)
            try:
                response = cast("httpx.Response", await requester(url, **request_kwargs))
            except httpx.RemoteProtocolError:
                # Stale keepalive reuse: agents older than the
                # AGENT_HTTP_KEEPALIVE_TIMEOUT_SEC fix run uvicorn's default 5s
                # keep-alive, shorter than our pool idle
                # (agent.http_pool_idle_seconds, up to 600s), so the pool can
                # return a connection the server already closed -> it disconnects
                # before reading our request. Upgraded agents hold keep-alives
                # for 630s (> pool idle max), so this should no longer trigger;
                # retained as defense-in-depth for mixed-version fleets. httpx
                # has evicted the dead connection, so one retry reuses the pool
                # on a fresh one. Only idempotent methods are safe (the request
                # never reached the server).
                if method_name not in _IDEMPOTENT_METHODS:
                    raise
                client_mode = "pooled_retried"
                response = cast("httpx.Response", await requester(url, **request_kwargs))
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and status_code >= 500:
            outcome = "http_error"
            await circuit_breaker.record_failure(host, error=f"HTTP {status_code}")
        else:
            await circuit_breaker.record_success(host)
        return response
    except httpx.HTTPError as exc:
        outcome_label, error_category = classify_httpx_transport(exc)
        exc_message = str(exc)
        breaker_error = f"{type(exc).__name__}: {exc_message}" if exc_message else type(exc).__name__
        await circuit_breaker.record_failure(host, error=breaker_error)
        outcome = outcome_label
        raise AgentUnreachableError(
            host,
            f"Cannot reach agent host {host}: {exc}",
            transport_outcome=outcome_label,
            error_category=error_category,
        ) from exc
    finally:
        record_agent_call(
            host=host,
            endpoint=endpoint,
            outcome=outcome,
            client_mode=client_mode,
            duration_seconds=perf_counter() - started,
        )
