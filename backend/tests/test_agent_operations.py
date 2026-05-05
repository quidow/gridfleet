from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from app.errors import AgentUnreachableError, CircuitOpenError
from app.services import agent_operations
from app.services.agent_circuit_breaker import agent_circuit_breaker

if TYPE_CHECKING:
    from app.agent_client import AgentClientFactory, QueryParams, RequestHeaders


def _response(method: str, url: str, *, status_code: int = 200, payload: object) -> httpx.Response:
    return httpx.Response(status_code, request=httpx.Request(method, url), json=payload)


class StrictAgentClient:
    def __init__(
        self,
        *,
        get_response: httpx.Response | None = None,
        post_response: httpx.Response | None = None,
        get_exception: httpx.HTTPError | None = None,
        post_exception: httpx.HTTPError | None = None,
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
    ) -> httpx.Response:
        self.get_calls.append(
            (
                url,
                {
                    "params": params,
                    "headers": headers,
                    "timeout": timeout,
                },
            )
        )
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
    ) -> httpx.Response:
        self.post_calls.append(
            (
                url,
                {
                    "params": params,
                    "headers": headers,
                    "json": json,
                    "timeout": timeout,
                },
            )
        )
        if self.post_exception is not None:
            raise self.post_exception
        return self.post_response


def _strict_client_factory(client: StrictAgentClient) -> AgentClientFactory:
    def factory(*, timeout: float | int) -> StrictAgentClient:
        del timeout
        return client

    return factory


async def test_agent_health_get_request_omits_json_body() -> None:
    client = StrictAgentClient(
        get_response=_response("GET", "http://10.0.0.5:5100/agent/health", payload={"status": "ok"})
    )

    payload = await agent_operations.agent_health(
        "10.0.0.5",
        5100,
        http_client_factory=_strict_client_factory(client),
        timeout=5,
    )

    assert payload == {"status": "ok"}
    assert client.get_calls == [
        (
            "http://10.0.0.5:5100/agent/health",
            {"params": None, "headers": {}, "timeout": 5},
        )
    ]


async def test_pack_device_health_get_request() -> None:
    client = StrictAgentClient(
        get_response=_response(
            "GET",
            "http://10.0.0.5:5100/agent/pack/devices/serial-1/health",
            payload={"healthy": True, "checks": []},
        )
    )

    payload = await agent_operations.pack_device_health(
        "10.0.0.5",
        5100,
        "serial-1",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        http_client_factory=_strict_client_factory(client),
        timeout=10,
    )

    assert payload["healthy"] is True
    assert client.get_calls == [
        (
            "http://10.0.0.5:5100/agent/pack/devices/serial-1/health",
            {
                "params": {
                    "pack_id": "appium-uiautomator2",
                    "platform_id": "android_mobile",
                    "device_type": "real_device",
                    "allow_boot": False,
                },
                "headers": {},
                "timeout": 10,
            },
        )
    ]


async def test_pack_device_health_forwards_headless_when_explicitly_requested() -> None:
    client = StrictAgentClient(
        get_response=_response(
            "GET",
            "http://10.0.0.5:5100/agent/pack/devices/emulator-5554/health",
            payload={"healthy": True},
        )
    )

    payload = await agent_operations.pack_device_health(
        "10.0.0.5",
        5100,
        "emulator-5554",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        allow_boot=True,
        headless=False,
        http_client_factory=_strict_client_factory(client),
        timeout=10,
    )

    assert payload["healthy"] is True
    assert client.get_calls == [
        (
            "http://10.0.0.5:5100/agent/pack/devices/emulator-5554/health",
            {
                "params": {
                    "pack_id": "appium-uiautomator2",
                    "platform_id": "android_mobile",
                    "device_type": "real_device",
                    "allow_boot": True,
                    "headless": False,
                },
                "headers": {},
                "timeout": 10,
            },
        )
    ]


async def test_appium_logs_get_request_omits_json_body() -> None:
    client = StrictAgentClient(
        get_response=_response(
            "GET",
            "http://10.0.0.5:5100/agent/appium/4723/logs",
            payload={"lines": ["one", "two"], "count": 2},
        )
    )

    payload = await agent_operations.appium_logs(
        "10.0.0.5",
        5100,
        4723,
        lines=200,
        http_client_factory=_strict_client_factory(client),
        timeout=10,
    )

    assert payload == {"lines": ["one", "two"], "count": 2}
    assert client.get_calls == [
        (
            "http://10.0.0.5:5100/agent/appium/4723/logs",
            {"params": {"lines": 200}, "headers": {}, "timeout": 10},
        )
    ]


async def test_appium_start_post_request_keeps_json_body() -> None:
    client = StrictAgentClient(
        post_response=_response("POST", "http://10.0.0.5:5100/agent/appium/start", payload={"port": 4723})
    )

    response = await agent_operations.appium_start(
        "http://10.0.0.5:5100",
        host="10.0.0.5",
        agent_port=5100,
        payload={"platform_id": "android_mobile"},
        http_client_factory=_strict_client_factory(client),
        timeout=15,
    )

    assert response.status_code == 200
    assert client.post_calls == [
        (
            "http://10.0.0.5:5100/agent/appium/start",
            {"params": None, "headers": {}, "json": {"platform_id": "android_mobile"}, "timeout": 15},
        )
    ]


async def test_get_tool_status_get_request_omits_json_body() -> None:
    client = StrictAgentClient(
        get_response=_response(
            "GET",
            "http://10.0.0.5:5100/agent/tools/status",
            payload={"node_provider": "fnm", "appium": "3.3.0"},
        )
    )

    payload = await agent_operations.get_tool_status(
        "10.0.0.5",
        5100,
        http_client_factory=_strict_client_factory(client),
        timeout=15,
    )

    assert payload["node_provider"] == "fnm"
    assert client.get_calls == [
        (
            "http://10.0.0.5:5100/agent/tools/status",
            {"params": None, "headers": {}, "timeout": 15},
        )
    ]


async def test_ensure_tools_post_request_keeps_json_body() -> None:
    client = StrictAgentClient(
        post_response=_response(
            "POST",
            "http://10.0.0.5:5100/agent/tools/ensure",
            payload={"appium": {"success": True}},
        )
    )

    payload = await agent_operations.ensure_tools(
        "10.0.0.5",
        5100,
        appium_version="3.3.0",
        selenium_jar_version="4.41.0",
        http_client_factory=_strict_client_factory(client),
        timeout=240,
    )

    assert payload["appium"]["success"] is True
    assert client.post_calls == [
        (
            "http://10.0.0.5:5100/agent/tools/ensure",
            {
                "params": None,
                "headers": {},
                "json": {
                    "appium_version": "3.3.0",
                    "selenium_jar_version": "4.41.0",
                },
                "timeout": 240,
            },
        )
    ]


async def test_pack_device_telemetry_returns_none_for_404() -> None:
    client = StrictAgentClient(
        get_response=_response(
            "GET",
            "http://10.0.0.5:5100/agent/pack/devices/missing/telemetry",
            status_code=404,
            payload={"detail": "not found"},
        )
    )

    payload = await agent_operations.pack_device_telemetry(
        "10.0.0.5",
        5100,
        "missing",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        device_type="real_device",
        connection_type=None,
        ip_address=None,
        http_client_factory=_strict_client_factory(client),
    )

    assert payload is None


async def test_agent_operations_short_circuit_after_repeated_transport_failures() -> None:
    current_time = 100.0

    def fake_monotonic() -> float:
        return current_time

    client = StrictAgentClient(
        get_exception=httpx.ConnectTimeout("boom", request=httpx.Request("GET", "http://10.0.0.8:5100/agent/health"))
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("app.services.agent_circuit_breaker.monotonic", fake_monotonic)

        for _ in range(agent_circuit_breaker.failure_threshold):
            with pytest.raises(AgentUnreachableError):
                await agent_operations.agent_health(
                    "10.0.0.8",
                    5100,
                    http_client_factory=_strict_client_factory(client),
                    timeout=5,
                )

        assert len(client.get_calls) == agent_circuit_breaker.failure_threshold

        with pytest.raises(CircuitOpenError):
            await agent_operations.agent_health(
                "10.0.0.8",
                5100,
                http_client_factory=_strict_client_factory(client),
                timeout=5,
            )

    assert len(client.get_calls) == agent_circuit_breaker.failure_threshold


async def test_get_pack_devices_raises_response_error_on_http_500() -> None:
    from app.errors import AgentResponseError

    client = StrictAgentClient(
        get_response=_response(
            "GET",
            "http://10.0.0.1:5100/agent/pack/devices",
            status_code=500,
            payload={"detail": "boom"},
        )
    )

    with pytest.raises(AgentResponseError) as exc_info:
        await agent_operations.get_pack_devices(
            "10.0.0.1",
            5100,
            http_client_factory=_strict_client_factory(client),
        )

    assert exc_info.value.http_status == 500
    assert exc_info.value.host == "10.0.0.1"
