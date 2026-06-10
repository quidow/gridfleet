from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import quote

import httpx
from pydantic import ValidationError as PydanticValidationError

from app.agent_comm.client import (
    AgentClientFactory,
    AgentHttpClient,
    JsonBody,
    QueryParams,
)
from app.agent_comm.client import (
    request as agent_request,
)
from app.agent_comm.generated import (
    AppiumLogsResponse,
    AppiumReconfigureResponse,
    AppiumStatusResponse,
    HealthResponse,
    HostTelemetryResponse,
    NormalizeDeviceResponse,
    PackDeviceHealthResponse,
    PackDeviceLifecycleResponse,
    PackDevicePropertiesResponse,
    PackDevicesResponse,
    PackDeviceTelemetryResponse,
    PluginListItem,
    PluginSyncResponse,
    ToolsStatusResponse,
)
from app.core.errors import AgentResponseError, AgentUnreachableError

if TYPE_CHECKING:
    import uuid

    from pydantic import BaseModel

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader

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
    settings: SettingsReader,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    params: QueryParams = None,
    json_body: JsonBody = None,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
) -> httpx.Response:
    auth = pool.auth if pool is not None else None
    use_pool = (
        pool is not None and http_client_factory is _DEFAULT_HTTP_CLIENT_FACTORY and _pool_enabled(settings=settings)
    )
    if use_pool:
        assert pool is not None  # narrowing for mypy
        max_keepalive = _settings_int("agent.http_pool_max_keepalive", default=10, settings=settings)
        idle_seconds = _settings_int("agent.http_pool_idle_seconds", default=60, settings=settings)
        client = await pool.get_client(
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
            circuit_breaker=circuit_breaker,
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
            circuit_breaker=circuit_breaker,
        )


def _pool_enabled(*, settings: SettingsReader) -> bool:
    try:
        return bool(settings.get("agent.http_pool_enabled"))
    except (KeyError, RuntimeError):
        return False


def _settings_int(key: str, *, default: int, settings: SettingsReader) -> int:
    try:
        return int(settings.get(key))
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


def _decode_model_payload(
    response: httpx.Response,
    *,
    host: str,
    action: str,
    model: type[BaseModel],
) -> dict[str, Any]:
    """Strict decode: HTTP error -> AgentResponseError; invalid JSON or payload -> AgentUnreachableError."""
    _raise_for_status(response, host=host, action=action)
    try:
        raw: dict[str, Any] = cast("dict[str, Any]", response.json())
    except ValueError as exc:
        raise AgentUnreachableError(host, f"Agent {action} failed on host {host} (invalid JSON payload)") from exc
    try:
        model.model_validate(raw)
    except PydanticValidationError as exc:
        raise AgentUnreachableError(host, f"Agent {action} failed on host {host} (invalid payload)") from exc
    return raw


def _decode_model_payload_lenient(
    response: httpx.Response,
    *,
    host: str,
    action: str,
    model: type[BaseModel],
    none_on_404: bool = False,
    require_200: bool = False,
) -> dict[str, Any] | None:
    """Lenient decode: invalid JSON or payload -> None.

    ``require_200`` returns None for ANY non-200 status without raising.
    ``none_on_404`` returns None for 404 and raises (via ``_raise_for_status``)
    for other error statuses.
    """
    if require_200:
        if response.status_code != 200:
            return None
    else:
        if none_on_404 and response.status_code == 404:
            return None
        _raise_for_status(response, host=host, action=action)
    try:
        raw: dict[str, Any] = cast("dict[str, Any]", response.json())
    except ValueError:
        return None
    try:
        model.model_validate(raw)
    except PydanticValidationError:
        return None
    return raw


def _as_dict(payload: object) -> dict[str, Any] | None:
    return payload if isinstance(payload, dict) else None


async def agent_health(
    host: str,
    agent_port: int,
    *,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 5,
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
) -> dict[str, Any] | None:
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/health",
        endpoint="agent_health",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        timeout=timeout,
        settings=settings,
        pool=pool,
        circuit_breaker=circuit_breaker,
    )
    return _decode_model_payload_lenient(response, host=host, action="health check", model=HealthResponse)


async def agent_host_telemetry(
    host: str,
    agent_port: int,
    *,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 5,
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
) -> dict[str, Any] | None:
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/host/telemetry",
        endpoint="agent_host_telemetry",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        timeout=timeout,
        settings=settings,
        pool=pool,
        circuit_breaker=circuit_breaker,
    )
    return _decode_model_payload_lenient(
        response, host=host, action="fetch host telemetry", model=HostTelemetryResponse, require_200=True
    )


async def appium_logs(
    host: str,
    agent_port: int,
    port: int,
    *,
    lines: int,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 10,
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
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
        settings=settings,
        pool=pool,
        circuit_breaker=circuit_breaker,
    )
    return _decode_model_payload(response, host=host, action="fetch Appium logs", model=AppiumLogsResponse)


async def appium_status(
    host: str,
    agent_port: int,
    port: int,
    *,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 5,
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
) -> dict[str, Any] | None:
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/appium/{port}/status",
        endpoint="appium_status",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        timeout=timeout,
        settings=settings,
        pool=pool,
        circuit_breaker=circuit_breaker,
    )
    return _decode_model_payload_lenient(
        response, host=host, action="fetch Appium status", model=AppiumStatusResponse, require_200=True
    )


async def appium_start(
    agent_base: str,
    *,
    host: str,
    agent_port: int,
    payload: dict[str, Any],
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int,
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
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
        settings=settings,
        pool=pool,
        circuit_breaker=circuit_breaker,
    )


async def appium_stop(
    agent_base: str,
    *,
    host: str,
    agent_port: int,
    port: int,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 10,
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
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
        settings=settings,
        pool=pool,
        circuit_breaker=circuit_breaker,
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
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
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
        settings=settings,
        pool=pool,
        circuit_breaker=circuit_breaker,
    )
    return _decode_model_payload(response, host=host, action="reconfigure Appium node", model=AppiumReconfigureResponse)


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
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
) -> list[dict[str, Any]]:
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/plugins",
        endpoint="plugins_list",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        timeout=timeout,
        settings=settings,
        pool=pool,
        circuit_breaker=circuit_breaker,
    )
    _raise_for_status(response, host=host, action="list plugins")
    try:
        raw: dict[str, Any] = cast("dict[str, Any]", response.json())
    except ValueError as exc:
        raise AgentUnreachableError(host, f"Agent list plugins failed on host {host} (invalid JSON payload)") from exc
    if not isinstance(raw, list):
        return []
    try:
        for it in raw:
            if isinstance(it, dict):
                PluginListItem.model_validate(it)
    except PydanticValidationError as exc:
        raise AgentUnreachableError(host, f"Agent list plugins failed on host {host} (invalid plugin list)") from exc
    return [it for it in raw if isinstance(it, dict)]


async def sync_plugins(
    host: str,
    agent_port: int,
    *,
    plugins: list[dict[str, Any]],
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 180,
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
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
        settings=settings,
        pool=pool,
        circuit_breaker=circuit_breaker,
    )
    return _decode_model_payload(response, host=host, action="sync plugins", model=PluginSyncResponse)


async def get_tool_status(
    host: str,
    agent_port: int,
    *,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 15,
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
) -> dict[str, Any]:
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/tools/status",
        endpoint="tools_status",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        timeout=timeout,
        settings=settings,
        pool=pool,
        circuit_breaker=circuit_breaker,
    )
    return _decode_model_payload(response, host=host, action="fetch tool status", model=ToolsStatusResponse)


async def get_pack_devices(
    host: str,
    agent_port: int,
    *,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = 45,
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
) -> dict[str, Any]:
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/pack/devices",
        endpoint="pack_devices",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        timeout=timeout,
        settings=settings,
        pool=pool,
        circuit_breaker=circuit_breaker,
    )
    return _decode_model_payload(response, host=host, action="list pack devices", model=PackDevicesResponse)


async def get_pack_device_properties(
    host: str,
    agent_port: int,
    connection_target: str,
    pack_id: str,
    *,
    identity_value: str | None = None,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = _PACK_ADAPTER_BACKEND_TIMEOUT,
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
) -> dict[str, Any] | None:
    params: dict[str, str] = {"pack_id": pack_id}
    if identity_value:
        params["identity_value"] = identity_value
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/pack/devices/{quote(connection_target, safe='')}/properties",
        endpoint="pack_device_properties",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        params=params,
        timeout=timeout,
        settings=settings,
        pool=pool,
        circuit_breaker=circuit_breaker,
    )
    return _decode_model_payload_lenient(
        response, host=host, action="fetch pack device properties", model=PackDevicePropertiesResponse, none_on_404=True
    )


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
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
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
        settings=settings,
        pool=pool,
        circuit_breaker=circuit_breaker,
    )
    if response.status_code == 404:
        return None
    return _decode_model_payload(response, host=host, action="normalize pack device", model=NormalizeDeviceResponse)


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
    identity_value: str | None = None,
    claimed_ports: dict[str, int] | None = None,
    has_live_session: bool | None = None,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = _PACK_ADAPTER_BACKEND_TIMEOUT,
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "pack_id": pack_id,
        "platform_id": platform_id,
        "device_type": device_type,
        "allow_boot": allow_boot,
    }
    if identity_value:
        params["identity_value"] = identity_value
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
    if claimed_ports is not None:
        params["claimed_ports"] = json.dumps(claimed_ports)
    if has_live_session is not None:
        params["has_live_session"] = has_live_session
    response = await _send_request(
        "GET",
        f"{agent_base_url(host, agent_port)}/agent/pack/devices/{quote(connection_target, safe='')}/health",
        endpoint="pack_device_health",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        params=params,
        timeout=timeout,
        settings=settings,
        pool=pool,
        circuit_breaker=circuit_breaker,
    )
    return _decode_model_payload(response, host=host, action="fetch pack device health", model=PackDeviceHealthResponse)


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
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
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
        settings=settings,
        pool=pool,
        circuit_breaker=circuit_breaker,
    )
    return _decode_model_payload_lenient(
        response, host=host, action="fetch pack device telemetry", model=PackDeviceTelemetryResponse, none_on_404=True
    )


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
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
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
        settings=settings,
        pool=pool,
        circuit_breaker=circuit_breaker,
    )
    action_label = f"run pack device lifecycle action {action}"
    return _decode_model_payload(response, host=host, action=action_label, model=PackDeviceLifecycleResponse)


async def pack_doctor(
    host: str,
    agent_port: int,
    pack_id: str,
    *,
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = _PACK_ADAPTER_BACKEND_TIMEOUT,
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
) -> list[dict[str, Any]]:
    response = await _send_request(
        "POST",
        f"{agent_base_url(host, agent_port)}/agent/pack/{quote(pack_id, safe='')}/doctor",
        endpoint="pack_doctor",
        host=host,
        agent_port=agent_port,
        http_client_factory=http_client_factory,
        timeout=timeout,
        settings=settings,
        pool=pool,
        circuit_breaker=circuit_breaker,
    )
    _raise_for_status(response, host=host, action="pack doctor")
    try:
        raw = cast("dict[str, Any]", response.json())
    except ValueError as exc:
        raise AgentUnreachableError(host, f"Agent pack doctor failed on host {host} (invalid JSON payload)") from exc
    checks = raw.get("checks", [])
    if not isinstance(checks, list):
        raise AgentUnreachableError(host, f"Agent pack doctor failed on host {host} (invalid checks payload)")
    return checks


def response_json_dict(response: httpx.Response) -> dict[str, Any]:
    payload = _as_dict(response.json())
    return payload if payload is not None else {}
