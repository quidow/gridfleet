"""Boundary contract: bad JSON from the agent surfaces as AgentUnreachableError."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from app.agent_comm import operations as agent_operations
from app.core.errors import AgentUnreachableError

if TYPE_CHECKING:
    from app.agent_comm.client import AgentClientFactory, QueryParams, RequestHeaders


def _text_response(method: str, url: str, *, status_code: int = 200, text: str) -> httpx.Response:
    return httpx.Response(status_code, request=httpx.Request(method, url), text=text)


def _json_response(method: str, url: str, *, status_code: int = 200, payload: object) -> httpx.Response:
    return httpx.Response(status_code, request=httpx.Request(method, url), json=payload)


class StrictAgentClient:
    def __init__(
        self,
        *,
        get_response: httpx.Response | None = None,
        post_response: httpx.Response | None = None,
    ) -> None:
        self.get_response = get_response or _json_response("GET", "http://example.test", payload={})
        self.post_response = post_response or _json_response("POST", "http://example.test", payload={})
        self.get_calls: list[tuple[str, dict[str, object]]] = []
        self.post_calls: list[tuple[str, dict[str, object]]] = []

    async def __aenter__(self) -> StrictAgentClient:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> bool:
        return False

    async def get(
        self,
        url: str,
        *,
        params: QueryParams = None,
        headers: RequestHeaders = None,
        timeout: float | int | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.Response:
        self.get_calls.append((url, {"params": params, "timeout": timeout}))
        return self.get_response

    async def post(
        self,
        url: str,
        *,
        params: QueryParams = None,
        headers: RequestHeaders = None,
        json: object | None = None,
        timeout: float | int | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.Response:
        self.post_calls.append((url, {"params": params, "json": json, "timeout": timeout}))
        return self.post_response


def _strict_client_factory(client: StrictAgentClient) -> AgentClientFactory:
    def factory(*, timeout: float | int) -> StrictAgentClient:
        del timeout
        return client

    return factory


_INVALID_JSON_TEXT = "<<not json>>"

# ---------------------------------------------------------------------------
# None-on-failure functions: JSONDecodeError -> return None
# ---------------------------------------------------------------------------


async def test_agent_health_returns_none_on_invalid_json() -> None:
    """agent_health should return None when the body is not valid JSON."""
    client = StrictAgentClient(
        get_response=_text_response("GET", "http://10.0.0.5:5100/agent/health", text=_INVALID_JSON_TEXT),
    )
    result = await agent_operations.agent_health(
        "10.0.0.5", 5100, http_client_factory=_strict_client_factory(client), timeout=5
    )
    assert result is None


async def test_agent_host_telemetry_returns_none_on_invalid_json() -> None:
    """agent_host_telemetry should return None when the body is not valid JSON."""
    client = StrictAgentClient(
        get_response=_text_response(
            "GET", "http://10.0.0.5:5100/agent/host/telemetry", status_code=200, text=_INVALID_JSON_TEXT
        ),
    )
    result = await agent_operations.agent_host_telemetry(
        "10.0.0.5", 5100, http_client_factory=_strict_client_factory(client), timeout=5
    )
    assert result is None


async def test_appium_status_returns_none_on_invalid_json() -> None:
    """appium_status should return None when the body is not valid JSON."""
    client = StrictAgentClient(
        get_response=_text_response(
            "GET", "http://10.0.0.5:5100/agent/appium/4723/status", status_code=200, text=_INVALID_JSON_TEXT
        ),
    )
    result = await agent_operations.appium_status(
        "10.0.0.5", 5100, 4723, http_client_factory=_strict_client_factory(client), timeout=5
    )
    assert result is None


async def test_get_pack_device_properties_returns_none_on_invalid_json() -> None:
    """get_pack_device_properties should return None when the body is not valid JSON."""
    client = StrictAgentClient(
        get_response=_text_response(
            "GET",
            "http://10.0.0.5:5100/agent/pack/devices/dev-1/properties",
            status_code=200,
            text=_INVALID_JSON_TEXT,
        ),
    )
    result = await agent_operations.get_pack_device_properties(
        "10.0.0.5", 5100, "dev-1", "appium-uiautomator2", http_client_factory=_strict_client_factory(client)
    )
    assert result is None


async def test_pack_device_telemetry_returns_none_on_invalid_json() -> None:
    """pack_device_telemetry should return None when the body is not valid JSON."""
    client = StrictAgentClient(
        get_response=_text_response(
            "GET",
            "http://10.0.0.5:5100/agent/pack/devices/dev-1/telemetry",
            status_code=200,
            text=_INVALID_JSON_TEXT,
        ),
    )
    result = await agent_operations.pack_device_telemetry(
        "10.0.0.5",
        5100,
        "dev-1",
        pack_id="appium-uiautomator2",
        platform_id="android",
        device_type="real_device",
        connection_type=None,
        ip_address=None,
        http_client_factory=_strict_client_factory(client),
    )
    assert result is None


# ---------------------------------------------------------------------------
# Raise-on-failure functions: JSONDecodeError -> AgentUnreachableError
# ---------------------------------------------------------------------------


async def test_pack_device_health_raises_on_invalid_json() -> None:
    """pack_device_health should raise AgentUnreachableError when the body is not valid JSON."""
    client = StrictAgentClient(
        get_response=_text_response(
            "GET",
            "http://10.0.0.5:5100/agent/pack/devices/dev-1/health",
            status_code=200,
            text=_INVALID_JSON_TEXT,
        ),
    )
    with pytest.raises(AgentUnreachableError):
        await agent_operations.pack_device_health(
            "10.0.0.5",
            5100,
            "dev-1",
            pack_id="appium-uiautomator2",
            platform_id="android",
            http_client_factory=_strict_client_factory(client),
        )


async def test_appium_logs_raises_on_invalid_json() -> None:
    """appium_logs should raise AgentUnreachableError when the body is not valid JSON."""
    client = StrictAgentClient(
        get_response=_text_response(
            "GET", "http://10.0.0.5:5100/agent/appium/4723/logs", status_code=200, text=_INVALID_JSON_TEXT
        ),
    )
    with pytest.raises(AgentUnreachableError):
        await agent_operations.appium_logs(
            "10.0.0.5", 5100, 4723, lines=50, http_client_factory=_strict_client_factory(client), timeout=10
        )


async def test_sync_plugins_raises_on_invalid_json() -> None:
    """sync_plugins should raise AgentUnreachableError when the body is not valid JSON."""
    client = StrictAgentClient(
        post_response=_text_response(
            "POST", "http://10.0.0.5:5100/agent/plugins/sync", status_code=200, text=_INVALID_JSON_TEXT
        ),
    )
    with pytest.raises(AgentUnreachableError):
        await agent_operations.sync_plugins(
            "10.0.0.5", 5100, plugins=[], http_client_factory=_strict_client_factory(client), timeout=30
        )


async def test_get_tool_status_raises_on_invalid_json() -> None:
    """get_tool_status should raise AgentUnreachableError when the body is not valid JSON."""
    client = StrictAgentClient(
        get_response=_text_response(
            "GET", "http://10.0.0.5:5100/agent/tools/status", status_code=200, text=_INVALID_JSON_TEXT
        ),
    )
    with pytest.raises(AgentUnreachableError):
        await agent_operations.get_tool_status(
            "10.0.0.5", 5100, http_client_factory=_strict_client_factory(client), timeout=15
        )


async def test_get_pack_devices_raises_on_invalid_json() -> None:
    """get_pack_devices should raise AgentUnreachableError when the body is not valid JSON."""
    client = StrictAgentClient(
        get_response=_text_response(
            "GET", "http://10.0.0.5:5100/agent/pack/devices", status_code=200, text=_INVALID_JSON_TEXT
        ),
    )
    with pytest.raises(AgentUnreachableError):
        await agent_operations.get_pack_devices("10.0.0.5", 5100, http_client_factory=_strict_client_factory(client))


async def test_list_plugins_raises_on_invalid_json() -> None:
    """list_plugins should raise AgentUnreachableError when the body is not valid JSON."""
    client = StrictAgentClient(
        get_response=_text_response(
            "GET", "http://10.0.0.5:5100/agent/plugins", status_code=200, text=_INVALID_JSON_TEXT
        ),
    )
    with pytest.raises(AgentUnreachableError):
        await agent_operations.list_plugins("10.0.0.5", 5100, http_client_factory=_strict_client_factory(client))


# ---------------------------------------------------------------------------
# Wire-shape preservation: get_pack_devices returns raw payload, no synthesized defaults
# ---------------------------------------------------------------------------


async def test_get_pack_devices_preserves_empty_agent_payload() -> None:
    """get_pack_devices must return the agent's literal JSON ({}) without synthesizing defaults."""
    client = StrictAgentClient(
        get_response=_json_response("GET", "http://10.0.0.5:5100/agent/pack/devices", payload={}),
    )
    result = await agent_operations.get_pack_devices(
        "10.0.0.5", 5100, http_client_factory=_strict_client_factory(client)
    )
    # {} from the agent must stay {} — NOT {"candidates": None}
    assert result == {}


async def test_agent_health_preserves_wire_shape() -> None:
    """agent_health must return the agent's literal JSON without model_dump synthesising defaults."""
    payload = {
        "status": "ok",
        "hostname": "agent.local",
        "os_type": "Linux",
        "version": "1.0.0",
        "version_guidance": {},
        "missing_prerequisites": [],
        "appium_processes": {},
        "capabilities": {},
    }
    client = StrictAgentClient(
        get_response=_json_response("GET", "http://10.0.0.5:5100/agent/health", payload=payload),
    )
    result = await agent_operations.agent_health("10.0.0.5", 5100, http_client_factory=_strict_client_factory(client))
    assert result is not None
    assert result["status"] == "ok"
    # Must not have extra synthesised keys injected by model_dump
    assert set(result.keys()) == set(payload.keys())
