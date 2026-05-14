from __future__ import annotations

import uuid
from typing import Any, Final, cast
from urllib.parse import quote

import httpx

from app.agent_comm.client import (
    AgentClientFactory,
    AgentHttpClient,
    JsonBody,
    QueryParams,
    _agent_basic_auth,
)
from app.agent_comm.client import (
    request as agent_request,
)
from app.agent_comm.http_pool import agent_http_pool
from app.errors import AgentResponseError, AgentUnreachableError
from app.settings import settings_service

_DEFAULT_HTTP_CLIENT_FACTORY = httpx.AsyncClient
type _AgentClientLike = AgentHttpClient | httpx.AsyncClient

# Backend per-call timeout for endpoints whose handlers invoke a driver-pack
# adapter probe via `dispatch_*`. Must stay above the agent's
# ADAPTER_HOOK_TIMEOUT_SECONDS (currently 30s in
# agent/agent_app/pack/adapter_dispatch.py); otherwise the backend
# ReadTimeouts before the agent finishes a slow probe, which trips the
# per-host circuit breaker even though the agent eventually returns 200.
# The 5s headroom over the 30s adapter ceiling covers HTTP overhead between
# the agent's adapter timer expiry and FastAPI returning the 504 to the
# backend. If ADAPTER_HOOK_TIMEOUT_SECONDS is raised, raise this in lockstep.
_PACK_ADAPTER_BACKEND_TIMEOUT: Final[int] = 35


def _as_agent_client(client: _AgentClientLike) -> AgentHttpClient:
    return cast("AgentHttpClient", client)


def agent_base_url(host: str, agent_port: int) -> str:
    return f"http://{host}:{agent_port}"


async def _send_request(
    method: str,
    url: str,
    *,
    endpoint: str,
    host: str,
    agent_port: int,
    timeout: float | int,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    params: QueryParams = None,
    json_body: JsonBody = None,
) -> httpx.Response:
    auth = _agent_basic_auth()
    use_pool = http_client_factory is _DEFAULT_HTTP_CLIENT_FACTORY and _pool_enabled()
    if use_pool:
        max_keepalive = _settings_int("agent.http_pool_max_keepalive", default=10)
        idle_seconds = _settings_int("agent.http_pool_idle_seconds", default=60)
        client = await agent_http_pool.get_client(
            host,
            agent_port,
            timeout=timeout,
            max_keepalive=max_keepalive,
            keepalive_expiry=idle_seconds,
        )
        return await agent_request(
            method,
            url,
            endpoint=endpoint,
            host=host,
            client_mode="pooled",
            client=_as_agent_client(client),
            params=params,
            json_body=json_body,
            timeout=timeout,
            auth=auth,
        )

    client_manager = http_client_factory(timeout=timeout)
    async with client_manager as fresh_client:
        return await agent_request(
            method,
            url,
            endpoint=endpoint,
            host=host,
            client_mode="fresh",
            client=_as_agent_client(fresh_client),
            params=params,
            json_body=json_body,
            timeout=timeout,
            auth=auth,
        )


def _pool_enabled() -> bool:
    try:
        return bool(settings_service.get("agent.http_pool_enabled"))
    except (KeyError, RuntimeError):
        return False


def _settings_int(key: str, *, default: int) -> int:
    try:
        return int(settings_service.get(key))
    except (KeyError, RuntimeError, TypeError, ValueError):
        return default


def _raise_for_status(response: httpx.Response, *, host: str, action: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code: int | None = exc.response.status_code if exc.response is not None else None
        status_label = str(status_code) if status_code is not None else "unknown"
        raise AgentResponseError(
            host,
            f"Agent {action} failed on host {host} (HTTP {status_label})",
            http_status=status_code,
        ) from exc


def _as_dict(payload: object) -> dict[str, Any] | None:
    return payload if isinstance(payload, dict) else None


def _as_list(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _response_error_detail(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text or None

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        if isinstance(detail, dict):
            message = detail.get("message")
            if isinstance(message, str) and message:
                return message
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message
    return None


async def agent_health(
    host: str,
    agent_port: int,
    *,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 5,
) -> dict[str, Any] | None:
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/health",
        endpoint="agent_health",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        timeout=timeout,
    )
    _raise_for_status(response, host=host, action="health check")
    return _as_dict(response.json())


async def agent_host_telemetry(
    host: str,
    agent_port: int,
    *,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 5,
) -> dict[str, Any] | None:
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/host/telemetry",
        endpoint="agent_host_telemetry",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        timeout=timeout,
    )
    if response.status_code != 200:
        return None
    return _as_dict(response.json())


async def appium_logs(
    host: str,
    agent_port: int,
    port: int,
    *,
    lines: int,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 10,
) -> dict[str, Any]:
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/appium/{port}/logs",
        endpoint="appium_logs",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        params={"lines": lines},
        timeout=timeout,
    )
    _raise_for_status(response, host=host, action="fetch Appium logs")
    payload = _as_dict(response.json())
    if payload is None:
        raise AgentUnreachableError(host, f"Agent fetch Appium logs failed on host {host} (invalid payload)")
    return payload


async def appium_status(
    host: str,
    agent_port: int,
    port: int,
    *,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 5,
) -> dict[str, Any] | None:
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/appium/{port}/status",
        endpoint="appium_status",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        timeout=timeout,
    )
    if response.status_code != 200:
        return None
    return _as_dict(response.json())


async def appium_start(
    agent_base: str,
    *,
    host: str,
    agent_port: int,
    payload: dict[str, Any],
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int,
) -> httpx.Response:
    return await _send_request(
        "POST",
        f"{agent_base}/agent/appium/start",
        endpoint="appium_start",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        json_body=payload,
        timeout=timeout,
    )


async def appium_stop(
    agent_base: str,
    *,
    host: str,
    agent_port: int,
    port: int,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 10,
) -> httpx.Response:
    return await _send_request(
        "POST",
        f"{agent_base}/agent/appium/stop",
        endpoint="appium_stop",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        json_body={"port": port},
        timeout=timeout,
    )


async def agent_appium_reconfigure(
    host: str,
    agent_port: int,
    *,
    port: int,
    accepting_new_sessions: bool,
    stop_pending: bool,
    grid_run_id: uuid.UUID | None,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 10,
) -> dict[str, Any]:
    response = await _send_request(
        "POST",
        f"{agent_base_url(host, agent_port)}/agent/appium/{port}/reconfigure",
        endpoint="appium_reconfigure",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        timeout=timeout,
        json_body={
            "accepting_new_sessions": accepting_new_sessions,
            "stop_pending": stop_pending,
            "grid_run_id": str(grid_run_id) if grid_run_id else None,
        },
    )
    _raise_for_status(response, host=host, action="reconfigure Appium node")
    payload = _as_dict(response.json())
    if payload is None:
        raise AgentUnreachableError(host, f"Agent reconfigure failed on host {host} (invalid payload)")
    return payload


async def grid_node_reregister(
    host: str,
    agent_port: int,
    node_id: uuid.UUID,
    *,
    target_run_id: uuid.UUID | None,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 20,
) -> uuid.UUID | None:
    response = await _send_request(
        "POST",
        f"{agent_base_url(host, agent_port)}/grid/node/{node_id}/reregister",
        endpoint="grid_node_reregister",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        json_body={"target_run_id": str(target_run_id) if target_run_id is not None else None},
        timeout=timeout,
    )
    _raise_for_status(response, host=host, action="re-register grid node")
    payload = _as_dict(response.json())
    if payload is None or "grid_run_id" not in payload:
        raise AgentUnreachableError(host, f"Agent grid node re-register failed on host {host} (invalid payload)")
    observed = payload["grid_run_id"]
    if observed is None:
        return None
    if not isinstance(observed, str):
        raise AgentUnreachableError(host, f"Agent grid node re-register failed on host {host} (invalid grid_run_id)")
    return uuid.UUID(observed)


def parse_agent_error_detail(response: httpx.Response | None) -> tuple[str | None, str]:
    """Return (code, message) parsed from an agent failure response."""
    if response is None:
        return None, "no response"
    try:
        payload = response.json()
    except ValueError:
        return None, response.text or f"HTTP {response.status_code}"
    if not isinstance(payload, dict):
        return None, str(payload)
    detail = payload.get("detail")
    if isinstance(detail, dict):
        code = detail.get("code") if isinstance(detail.get("code"), str) else None
        message = detail.get("message")
        return code, str(message) if message is not None else str(detail)
    return None, str(detail) if detail is not None else f"HTTP {response.status_code}"


async def list_plugins(
    host: str,
    agent_port: int,
    *,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 15,
) -> list[dict[str, Any]]:
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/plugins",
        endpoint="plugins_list",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        timeout=timeout,
    )
    _raise_for_status(response, host=host, action="list plugins")
    return _as_list(response.json())


async def sync_plugins(
    host: str,
    agent_port: int,
    *,
    plugins: list[dict[str, Any]],
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 180,
) -> dict[str, Any]:
    response = await _send_request(
        "POST",
        f"{agent_base_url(host, agent_port)}/agent/plugins/sync",
        endpoint="plugins_sync",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        json_body={"plugins": plugins},
        timeout=timeout,
    )
    _raise_for_status(response, host=host, action="sync plugins")
    payload = _as_dict(response.json())
    if payload is None:
        raise AgentUnreachableError(host, f"Agent sync plugins failed on host {host} (invalid payload)")
    return payload


async def get_tool_status(
    host: str,
    agent_port: int,
    *,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 15,
) -> dict[str, Any]:
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/tools/status",
        endpoint="tools_status",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        timeout=timeout,
    )
    _raise_for_status(response, host=host, action="fetch tool status")
    payload = _as_dict(response.json())
    if payload is None:
        raise AgentUnreachableError(host, f"Agent fetch tool status failed on host {host} (invalid payload)")
    return payload


async def get_pack_devices(
    host: str,
    agent_port: int,
    *,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 45,
) -> dict[str, Any]:
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/pack/devices",
        endpoint="pack_devices",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        timeout=timeout,
    )
    _raise_for_status(response, host=host, action="list pack devices")
    return _as_dict(response.json()) or {}


async def get_pack_device_properties(
    host: str,
    agent_port: int,
    connection_target: str,
    pack_id: str,
    *,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = _PACK_ADAPTER_BACKEND_TIMEOUT,
) -> dict[str, Any] | None:
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/pack/devices/{quote(connection_target, safe='')}/properties",
        endpoint="pack_device_properties",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        params={"pack_id": pack_id},
        timeout=timeout,
    )
    if response.status_code == 404:
        return None
    _raise_for_status(response, host=host, action="fetch pack device properties")
    return _as_dict(response.json())


async def normalize_pack_device(
    host: str,
    agent_port: int,
    *,
    pack_id: str,
    pack_release: str,
    platform_id: str,
    raw_input: dict[str, Any],
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = _PACK_ADAPTER_BACKEND_TIMEOUT,
) -> dict[str, Any] | None:
    response = await _send_request(
        "POST",
        f"{agent_base_url(host, agent_port)}/agent/pack/devices/normalize",
        endpoint="pack_device_normalize",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        json_body={
            "pack_id": pack_id,
            "pack_release": pack_release,
            "platform_id": platform_id,
            "raw_input": raw_input,
        },
        timeout=timeout,
    )
    if response.status_code == 404:
        return None
    _raise_for_status(response, host=host, action="normalize pack device")
    return _as_dict(response.json())


async def pack_device_health(
    host: str,
    agent_port: int,
    connection_target: str,
    *,
    pack_id: str,
    platform_id: str,
    device_type: str = "real_device",
    connection_type: str | None = None,
    ip_address: str | None = None,
    allow_boot: bool = False,
    headless: bool | None = None,
    ip_ping_timeout_sec: float | None = None,
    ip_ping_count: int | None = None,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = _PACK_ADAPTER_BACKEND_TIMEOUT,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "pack_id": pack_id,
        "platform_id": platform_id,
        "device_type": device_type,
        "allow_boot": allow_boot,
    }
    if connection_type is not None:
        params["connection_type"] = connection_type
    if ip_address is not None:
        params["ip_address"] = ip_address
    if headless is not None:
        params["headless"] = headless
    if ip_ping_timeout_sec is not None:
        params["ip_ping_timeout_sec"] = ip_ping_timeout_sec
    if ip_ping_count is not None:
        params["ip_ping_count"] = ip_ping_count
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/pack/devices/{quote(connection_target, safe='')}/health",
        endpoint="pack_device_health",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        params=params,
        timeout=timeout,
    )
    _raise_for_status(response, host=host, action="fetch pack device health")
    payload = _as_dict(response.json())
    if payload is None:
        raise AgentUnreachableError(host, f"Agent fetch pack device health failed on host {host} (invalid payload)")
    return payload


async def pack_device_telemetry(
    host: str,
    agent_port: int,
    connection_target: str,
    *,
    pack_id: str,
    platform_id: str,
    device_type: str,
    connection_type: str | None,
    ip_address: str | None,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = _PACK_ADAPTER_BACKEND_TIMEOUT,
) -> dict[str, Any] | None:
    params: dict[str, Any] = {
        "pack_id": pack_id,
        "platform_id": platform_id,
        "device_type": device_type,
    }
    if connection_type is not None:
        params["connection_type"] = connection_type
    if ip_address is not None:
        params["ip_address"] = ip_address
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/pack/devices/{quote(connection_target, safe='')}/telemetry",
        endpoint="pack_device_telemetry",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        params=params,
        timeout=timeout,
    )
    if response.status_code == 404:
        return None
    _raise_for_status(response, host=host, action="fetch pack device telemetry")
    return _as_dict(response.json())


async def pack_device_lifecycle_action(
    host: str,
    agent_port: int,
    connection_target: str,
    *,
    pack_id: str,
    platform_id: str,
    action: str,
    args: dict[str, Any] | None = None,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = _PACK_ADAPTER_BACKEND_TIMEOUT,
) -> dict[str, Any]:
    response = await _send_request(
        "POST",
        f"{agent_base_url(host, agent_port)}/agent/pack/devices/{quote(connection_target, safe='')}/lifecycle/{action}",
        endpoint="pack_device_lifecycle_action",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        params={"pack_id": pack_id, "platform_id": platform_id},
        json_body=args or {},
        timeout=timeout,
    )
    _raise_for_status(response, host=host, action=f"run pack device lifecycle action {action}")
    payload = _as_dict(response.json())
    if payload is None:
        raise AgentUnreachableError(host, f"Agent lifecycle action {action} failed on host {host} (invalid payload)")
    return payload


def response_json_dict(response: httpx.Response) -> dict[str, Any]:
    payload = _as_dict(response.json())
    return payload if payload is not None else {}
