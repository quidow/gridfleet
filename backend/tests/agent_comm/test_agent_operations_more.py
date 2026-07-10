from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx2 as httpx
import pytest

from app.agent_comm import operations as agent_operations
from app.core.errors import AgentResponseError, AgentUnreachableError
from tests.fakes import FakeSettingsReader

if TYPE_CHECKING:
    from app.agent_comm.client import AgentClientFactory, QueryParams, RequestHeaders

SETTINGS = FakeSettingsReader()


def _response(method: str, url: str, *, status_code: int = 200, payload: object) -> httpx.Response:
    return httpx.Response(status_code, request=httpx.Request(method, url), json=payload)


def _text_response(method: str, url: str, *, status_code: int, text: str) -> httpx.Response:
    return httpx.Response(status_code, request=httpx.Request(method, url), text=text)


class StrictAgentClient:
    def __init__(
        self,
        *,
        get_response: httpx.Response | None = None,
        post_response: httpx.Response | None = None,
        get_exception: Exception | None = None,
        post_exception: Exception | None = None,
    ) -> None:
        self.get_response = get_response or _response("GET", "http://example.test", payload={})
        self.post_response = post_response or _response("POST", "http://example.test", payload={})
        self.get_exception = get_exception
        self.post_exception = post_exception
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
        recorded: dict[str, object] = {"params": params, "headers": headers, "timeout": timeout}
        if auth is not None:
            recorded["auth"] = auth
        self.get_calls.append((url, recorded))
        if self.get_exception is not None:
            raise self.get_exception
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
        recorded: dict[str, object] = {"params": params, "headers": headers, "json": json, "timeout": timeout}
        if auth is not None:
            recorded["auth"] = auth
        self.post_calls.append((url, recorded))
        if self.post_exception is not None:
            raise self.post_exception
        return self.post_response


def _strict_client_factory(client: StrictAgentClient) -> AgentClientFactory:
    def factory(*, timeout: float | int) -> StrictAgentClient:
        del timeout
        return client

    return factory


def test_pack_device_health_percent_encodes_connection_target() -> None:
    """The pack health URL should percent-encode the connection target."""
    from urllib.parse import quote

    assert quote("serial/with spaces", safe="") == "serial%2Fwith%20spaces"


def test_parse_agent_error_detail_handles_all_payload_shapes() -> None:
    assert agent_operations.parse_agent_error_detail(None) == (None, "no response")
    assert agent_operations.parse_agent_error_detail(
        _text_response("GET", "http://example.test", status_code=502, text="plain")
    ) == (None, "plain")
    assert agent_operations.parse_agent_error_detail(_response("GET", "http://example.test", payload=["bad"])) == (
        None,
        "['bad']",
    )
    assert agent_operations.parse_agent_error_detail(
        _response("GET", "http://example.test", payload={"detail": {"code": "port_occupied", "message": "busy"}})
    ) == ("port_occupied", "busy")
    assert agent_operations.parse_agent_error_detail(
        _response("GET", "http://example.test", status_code=503, payload={})
    ) == (None, "HTTP 503")


def test_raise_for_status_wraps_http_errors() -> None:
    response = _response("GET", "http://example.test", status_code=503, payload={"detail": "boom"})

    with pytest.raises(AgentResponseError, match="HTTP 503") as exc_info:
        agent_operations._raise_for_status(response, host="10.0.0.5", action="demo")

    assert exc_info.value.http_status == 503
    assert exc_info.value.host == "10.0.0.5"


async def test_normalize_pack_device_returns_none_for_404() -> None:
    client = StrictAgentClient(
        post_response=_response(
            "POST",
            "http://10.0.0.5:5100/agent/pack/devices/normalize",
            status_code=404,
            payload={"detail": "not found"},
        )
    )

    assert (
        await agent_operations.normalize_pack_device(
            "10.0.0.5",
            5100,
            pack_id="pack",
            pack_release="1",
            platform_id="android",
            raw_input={},
            http_client_factory=_strict_client_factory(client),
            circuit_breaker=AsyncMock(before_request=AsyncMock(return_value=None)),
        )
        is None
    )


async def test_get_pack_devices_uses_discovery_timeout() -> None:
    client = StrictAgentClient(
        get_response=_response(
            "GET",
            "http://10.0.0.5:5100/agent/pack/devices",
            payload={"candidates": []},
        )
    )

    payload = await agent_operations.get_pack_devices(
        "10.0.0.5",
        5100,
        http_client_factory=_strict_client_factory(client),
        circuit_breaker=AsyncMock(before_request=AsyncMock(return_value=None)),
    )

    assert payload == {"candidates": []}
    assert client.get_calls == [
        (
            "http://10.0.0.5:5100/agent/pack/devices",
            {"params": None, "headers": {}, "timeout": 45},
        )
    ]


async def test_appium_status_returns_none_for_non_200() -> None:
    client = StrictAgentClient(
        get_response=_response(
            "GET",
            "http://10.0.0.5:5100/agent/appium/4723/status",
            status_code=503,
            payload={"detail": "starting"},
        )
    )

    payload = await agent_operations.appium_status(
        "10.0.0.5",
        5100,
        4723,
        http_client_factory=_strict_client_factory(client),
        circuit_breaker=AsyncMock(before_request=AsyncMock(return_value=None)),
    )

    assert payload is None


async def test_appium_status_returns_none_for_invalid_payload() -> None:
    """appium_status is a None-on-failure endpoint: invalid payload -> None, not a raise."""
    client = StrictAgentClient(
        get_response=_response(
            "GET",
            "http://10.0.0.5:5100/agent/appium/4723/status",
            payload=["bad"],
        )
    )

    result = await agent_operations.appium_status(
        "10.0.0.5",
        5100,
        4723,
        http_client_factory=_strict_client_factory(client),
        circuit_breaker=AsyncMock(before_request=AsyncMock(return_value=None)),
    )
    assert result is None


async def test_agent_health_raises_response_error_for_non_200() -> None:
    from app.core.errors import AgentResponseError

    client = StrictAgentClient(
        get_response=_response("GET", "http://10.0.0.5:5100/agent/health", status_code=503, payload={"detail": "down"})
    )

    with pytest.raises(AgentResponseError) as caught:
        await agent_operations.agent_health(
            "10.0.0.5",
            5100,
            http_client_factory=_strict_client_factory(client),
            circuit_breaker=AsyncMock(before_request=AsyncMock(return_value=None)),
        )

    assert caught.value.http_status == 503


async def test_pack_device_health_and_lifecycle_raise_for_invalid_payload() -> None:
    health_client = StrictAgentClient(
        get_response=_response("GET", "http://10.0.0.5:5100/agent/pack/devices/demo/health", payload=["bad"])
    )
    lifecycle_client = StrictAgentClient(
        post_response=_response(
            "POST", "http://10.0.0.5:5100/agent/pack/devices/demo/lifecycle/reconnect", payload=["bad"]
        )
    )

    with pytest.raises(AgentUnreachableError, match="fetch pack device health failed"):
        await agent_operations.pack_device_health(
            "10.0.0.5",
            5100,
            "demo",
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            http_client_factory=_strict_client_factory(health_client),
            circuit_breaker=AsyncMock(before_request=AsyncMock(return_value=None)),
        )

    with pytest.raises(AgentUnreachableError, match="lifecycle action reconnect failed"):
        await agent_operations.pack_device_lifecycle_action(
            "10.0.0.5",
            5100,
            "demo",
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            action="reconnect",
            args={"ip_address": "10.0.0.20"},
            http_client_factory=_strict_client_factory(lifecycle_client),
            circuit_breaker=AsyncMock(before_request=AsyncMock(return_value=None)),
        )


async def test_pack_device_health_includes_optional_probe_params() -> None:
    client = StrictAgentClient(
        get_response=_response(
            "GET",
            "http://10.0.0.5:5100/agent/pack/devices/demo/health",
            payload={"healthy": True, "checks": []},
        )
    )

    result = await agent_operations.pack_device_health(
        "10.0.0.5",
        5100,
        "demo",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        connection_type="network",
        ip_address="10.0.0.9",
        headless=True,
        ip_ping_timeout_sec=1.5,
        ip_ping_count=3,
        http_client_factory=_strict_client_factory(client),
        circuit_breaker=AsyncMock(before_request=AsyncMock(return_value=None)),
    )
    assert result["healthy"] is True
    params = client.get_calls[0][1]["params"]
    assert params["connection_type"] == "network"
    assert params["ip_address"] == "10.0.0.9"
    assert params["headless"] is True
    assert params["ip_ping_timeout_sec"] == 1.5
    assert params["ip_ping_count"] == 3


async def test_appium_logs_and_tool_status_raise_for_invalid_payload() -> None:
    logs_client = StrictAgentClient(
        get_response=_response("GET", "http://10.0.0.5:5100/agent/appium/4723/logs", payload=["bad"])
    )
    tool_status_client = StrictAgentClient(
        get_response=_response("GET", "http://10.0.0.5:5100/agent/tools/status", payload=["bad"])
    )

    with pytest.raises(AgentUnreachableError, match="fetch Appium logs failed"):
        await agent_operations.appium_logs(
            "10.0.0.5",
            5100,
            4723,
            lines=10,
            http_client_factory=_strict_client_factory(logs_client),
            circuit_breaker=AsyncMock(before_request=AsyncMock(return_value=None)),
        )

    with pytest.raises(AgentUnreachableError, match="fetch tool status failed"):
        await agent_operations.get_tool_status(
            "10.0.0.5",
            5100,
            http_client_factory=_strict_client_factory(tool_status_client),
            circuit_breaker=AsyncMock(before_request=AsyncMock(return_value=None)),
        )


async def test_pack_device_lifecycle_resolve_raises_for_invalid_payload() -> None:
    client = StrictAgentClient(
        post_response=_response(
            "POST",
            "http://10.0.0.5:5100/agent/pack/devices/192.168.1.10%3A5555/lifecycle/resolve",
            payload=["bad"],
        )
    )

    with pytest.raises(AgentUnreachableError, match="lifecycle action resolve failed"):
        await agent_operations.pack_device_lifecycle_action(
            "10.0.0.5",
            5100,
            "192.168.1.10:5555",
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            action="resolve",
            http_client_factory=_strict_client_factory(client),
            circuit_breaker=AsyncMock(before_request=AsyncMock(return_value=None)),
        )


def test_response_json_dict_returns_empty_dict_for_non_mapping_payload() -> None:
    response = _response("GET", "http://example.test", payload=["not", "a", "dict"])
    assert agent_operations.response_json_dict(response) == {}
