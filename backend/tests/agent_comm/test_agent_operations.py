from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx2 as httpx
import pytest

from app.agent_comm import operations as agent_operations
from app.agent_comm.circuit_breaker import AgentCircuitBreaker
from app.core.errors import AgentUnreachableError, CircuitOpenError
from tests.fakes import FakeSettingsReader

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.agent_comm.client import AgentClientFactory, QueryParams, RequestHeaders


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
        recorded: dict[str, object] = {
            "params": params,
            "headers": headers,
            "json": json,
            "timeout": timeout,
        }
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


_VALID_HEALTH_PAYLOAD: dict[str, object] = {
    "status": "ok",
    "hostname": "agent.local",
    "os_type": "Linux",
    "version": "1.0.0",
    "registered": True,
    "version_guidance": {},
    "missing_prerequisites": [],
    "appium_processes": {},
    "capabilities": {},
}
SETTINGS = FakeSettingsReader()


def _noop_breaker() -> AsyncMock:
    breaker = AsyncMock()
    breaker.before_request = AsyncMock(return_value=None)
    return breaker


async def test_agent_health_get_request_omits_json_body() -> None:
    client = StrictAgentClient(
        get_response=_response("GET", "http://10.0.0.5:5100/agent/health", payload=_VALID_HEALTH_PAYLOAD)
    )

    payload = await agent_operations.agent_health(
        "10.0.0.5",
        5100,
        http_client_factory=_strict_client_factory(client),
        timeout=5,
        settings=SETTINGS,
        circuit_breaker=_noop_breaker(),
    )

    assert payload is not None
    assert payload["status"] == "ok"


async def test_agent_health_raises_response_error_on_http_500() -> None:
    """5xx must surface as AgentResponseError with http_status, not silently return None."""
    from app.core.errors import AgentResponseError

    client = StrictAgentClient(
        get_response=_response(
            "GET",
            "http://10.0.0.5:5100/agent/health",
            status_code=503,
            payload={"detail": "agent boot in progress"},
        )
    )

    with pytest.raises(AgentResponseError) as caught:
        await agent_operations.agent_health(
            "10.0.0.5",
            5100,
            http_client_factory=_strict_client_factory(client),
            timeout=5,
            settings=SETTINGS,
            circuit_breaker=_noop_breaker(),
        )

    assert caught.value.http_status == 503
    assert client.get_calls == [
        (
            "http://10.0.0.5:5100/agent/health",
            {"params": None, "headers": {}, "timeout": 5},
        )
    ]


async def test_agent_nodes_refresh_posts_with_no_body() -> None:
    client = StrictAgentClient(
        post_response=_response("POST", "http://10.0.0.5:5100/agent/appium-nodes/refresh", payload={"accepted": True})
    )

    await agent_operations.agent_nodes_refresh(
        "10.0.0.5",
        5100,
        http_client_factory=_strict_client_factory(client),
        settings=SETTINGS,
        circuit_breaker=_noop_breaker(),
    )

    assert client.post_calls == [
        (
            "http://10.0.0.5:5100/agent/appium-nodes/refresh",
            {"params": None, "headers": {}, "json": None, "timeout": agent_operations.NODE_POKE_TIMEOUT_SEC},
        )
    ]


async def test_agent_nodes_refresh_raises_response_error_on_http_500() -> None:
    from app.core.errors import AgentResponseError

    client = StrictAgentClient(
        post_response=_response(
            "POST", "http://10.0.0.5:5100/agent/appium-nodes/refresh", status_code=503, payload={"detail": "busy"}
        )
    )

    with pytest.raises(AgentResponseError) as caught:
        await agent_operations.agent_nodes_refresh(
            "10.0.0.5",
            5100,
            http_client_factory=_strict_client_factory(client),
            settings=SETTINGS,
            circuit_breaker=_noop_breaker(),
        )

    assert caught.value.http_status == 503


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
        settings=SETTINGS,
        circuit_breaker=_noop_breaker(),
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


async def test_pack_device_health_forwards_ip_ping_params() -> None:
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
        ip_ping_timeout_sec=1.5,
        ip_ping_count=2,
        http_client_factory=_strict_client_factory(client),
        timeout=10,
        settings=SETTINGS,
        circuit_breaker=_noop_breaker(),
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
                    "ip_ping_timeout_sec": 1.5,
                    "ip_ping_count": 2,
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
        settings=SETTINGS,
        circuit_breaker=_noop_breaker(),
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
            payload={"lines": ["one", "two"], "count": 2, "port": 4723},
        )
    )

    payload = await agent_operations.appium_logs(
        "10.0.0.5",
        5100,
        4723,
        lines=200,
        http_client_factory=_strict_client_factory(client),
        timeout=10,
        settings=SETTINGS,
        circuit_breaker=_noop_breaker(),
    )

    assert payload["lines"] == ["one", "two"]
    assert payload["count"] == 2
    assert payload["port"] == 4723
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
        settings=SETTINGS,
        circuit_breaker=_noop_breaker(),
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
            payload={
                "host": {
                    "node": {"name": "Node", "version": "24.14.1", "description": "JS runtime"},
                    "node_provider": {"name": "Node Provider", "version": "fnm", "description": "Node manager"},
                },
                "packs": {},
            },
        )
    )

    payload = await agent_operations.get_tool_status(
        "10.0.0.5",
        5100,
        http_client_factory=_strict_client_factory(client),
        timeout=15,
        settings=SETTINGS,
        circuit_breaker=_noop_breaker(),
    )

    assert payload["host"]["node_provider"]["version"] == "fnm"
    assert client.get_calls == [
        (
            "http://10.0.0.5:5100/agent/tools/status",
            {"params": None, "headers": {}, "timeout": 15},
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
        settings=SETTINGS,
        circuit_breaker=_noop_breaker(),
    )

    assert payload is None


async def test_agent_operations_short_circuit_after_repeated_transport_failures() -> None:
    current_time = 100.0

    def fake_monotonic() -> float:
        return current_time

    client = StrictAgentClient(
        get_exception=httpx.ConnectTimeout("boom", request=httpx.Request("GET", "http://10.0.0.8:5100/agent/health"))
    )

    threshold = 5  # default agent.circuit_breaker_failure_threshold
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("app.agent_comm.circuit_breaker.monotonic", fake_monotonic)
        monkeypatch.setattr("app.agent_comm.circuit_breaker._resolve_host_identity", AsyncMock(return_value={}))
        breaker = AgentCircuitBreaker(publisher=AsyncMock(), settings=SETTINGS, session_factory=AsyncMock())
        monkeypatch.setattr(breaker, "failure_threshold", lambda: threshold)
        monkeypatch.setattr(breaker, "cooldown_seconds", lambda: 30.0)

        for _ in range(threshold):
            with pytest.raises(AgentUnreachableError):
                await agent_operations.agent_health(
                    "10.0.0.8",
                    5100,
                    http_client_factory=_strict_client_factory(client),
                    timeout=5,
                    settings=SETTINGS,
                    circuit_breaker=breaker,
                )

        assert len(client.get_calls) == threshold

        with pytest.raises(CircuitOpenError):
            await agent_operations.agent_health(
                "10.0.0.8",
                5100,
                http_client_factory=_strict_client_factory(client),
                timeout=5,
                settings=SETTINGS,
                circuit_breaker=breaker,
            )

    assert len(client.get_calls) == threshold


async def test_get_pack_devices_raises_response_error_on_http_500() -> None:
    from app.core.errors import AgentResponseError

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
            settings=SETTINGS,
            circuit_breaker=_noop_breaker(),
        )

    assert exc_info.value.http_status == 500
    assert exc_info.value.host == "10.0.0.1"


async def test_agent_request_passes_auth() -> None:
    from app.agent_comm.client import request as agent_request

    client = StrictAgentClient()
    auth = httpx.BasicAuth("ops", "secret")

    await agent_request(
        "GET",
        "http://host.test/agent/health",
        endpoint="agent_health",
        host="host.test",
        client=client,
        auth=auth,
        circuit_breaker=_noop_breaker(),
    )

    assert client.get_calls, "expected one GET call"
    _, kwargs = client.get_calls[0]
    assert kwargs["auth"] is auth


async def test_agent_request_omits_auth_when_not_passed() -> None:
    """client.request() no longer reads settings; when auth is not supplied, no auth is applied."""
    from app.agent_comm.client import request as agent_request

    client = StrictAgentClient()

    await agent_request(
        "GET",
        "http://host.test/agent/health",
        endpoint="agent_health",
        host="host.test",
        client=client,
        circuit_breaker=_noop_breaker(),
    )

    assert client.get_calls, "expected one GET call"
    _, kwargs = client.get_calls[0]
    assert "auth" not in kwargs


def _make_capturing_factory(captured: list[httpx.Auth | None]) -> Callable[..., StrictAgentClient]:
    class CapturingClient(StrictAgentClient):
        async def get(
            self,
            url: str,
            *,
            params: QueryParams = None,
            headers: RequestHeaders = None,
            timeout: float | int | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.Response:
            captured.append(auth)
            return await super().get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
                auth=auth,
            )

    def factory(**_kwargs: object) -> StrictAgentClient:
        return CapturingClient()

    return factory


async def test_send_request_supplies_basic_auth_when_configured() -> None:
    expected_auth = httpx.BasicAuth("ops", "secret")
    from app.agent_comm.http_pool import AgentHttpPool

    pool = AgentHttpPool(agent_auth=expected_auth)
    captured: list[httpx.Auth | None] = []
    factory = _make_capturing_factory(captured)
    await agent_operations.agent_health(
        "host.test",
        agent_port=5100,
        http_client_factory=factory,
        settings=SETTINGS,
        pool=pool,
        circuit_breaker=_noop_breaker(),
    )
    assert captured == [expected_auth]


async def test_send_request_omits_auth_when_unset() -> None:
    captured: list[httpx.Auth | None] = []
    factory = _make_capturing_factory(captured)
    await agent_operations.agent_health(
        "host.test",
        agent_port=5100,
        http_client_factory=factory,
        settings=SETTINGS,
        circuit_breaker=_noop_breaker(),
    )
    assert captured == [None]
