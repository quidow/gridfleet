from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from app.errors import AgentResponseError, AgentUnreachableError
from app.services import agent_operations

if TYPE_CHECKING:
    from app.agent_client import AgentClientFactory, QueryParams, RequestHeaders


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
    ) -> httpx.Response:
        self.get_calls.append((url, {"params": params, "headers": headers, "timeout": timeout}))
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
        self.post_calls.append((url, {"params": params, "headers": headers, "json": json, "timeout": timeout}))
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


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_response("GET", "http://example.test", payload={"detail": "boom"}), "boom"),
        (_response("GET", "http://example.test", payload={"message": "oops"}), "oops"),
        (_text_response("GET", "http://example.test", status_code=500, text="plain error"), "plain error"),
    ],
)
def test_response_error_detail_extracts_useful_message(response: httpx.Response, expected: str) -> None:
    assert agent_operations._response_error_detail(response) == expected


def test_response_error_detail_returns_none_for_unstructured_payload() -> None:
    response = _response("GET", "http://example.test", payload={"detail": []})
    assert agent_operations._response_error_detail(response) is None


def test_raise_for_status_wraps_http_errors() -> None:
    response = _response("GET", "http://example.test", status_code=503, payload={"detail": "boom"})

    with pytest.raises(AgentResponseError, match="HTTP 503") as exc_info:
        agent_operations._raise_for_status(response, host="10.0.0.5", action="demo")

    assert exc_info.value.http_status == 503
    assert exc_info.value.host == "10.0.0.5"


def test_as_list_returns_empty_for_non_list_payload() -> None:
    assert agent_operations._as_list({"bad": True}) == []


async def test_pack_device_properties_returns_none_for_404() -> None:
    client = StrictAgentClient(
        get_response=_response(
            "GET",
            "http://10.0.0.5:5100/agent/pack/devices/demo/properties",
            status_code=404,
            payload={"detail": "not found"},
        )
    )

    payload = await agent_operations.get_pack_device_properties(
        "10.0.0.5",
        5100,
        "demo",
        "appium-uiautomator2",
        http_client_factory=_strict_client_factory(client),
    )

    assert payload is None


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
    )

    assert payload is None


async def test_agent_health_returns_none_for_non_200() -> None:
    client = StrictAgentClient(
        get_response=_response("GET", "http://10.0.0.5:5100/agent/health", status_code=503, payload={"detail": "down"})
    )

    payload = await agent_operations.agent_health(
        "10.0.0.5",
        5100,
        http_client_factory=_strict_client_factory(client),
    )

    assert payload is None


async def test_agent_host_telemetry_returns_payload() -> None:
    client = StrictAgentClient(
        get_response=_response(
            "GET",
            "http://10.0.0.5:5100/agent/host/telemetry",
            payload={"cpu_percent": 71.2},
        )
    )

    payload = await agent_operations.agent_host_telemetry(
        "10.0.0.5",
        5100,
        http_client_factory=_strict_client_factory(client),
    )

    assert payload == {"cpu_percent": 71.2}


async def test_agent_host_telemetry_returns_none_for_non_200() -> None:
    client = StrictAgentClient(
        get_response=_response(
            "GET",
            "http://10.0.0.5:5100/agent/host/telemetry",
            status_code=404,
            payload={"detail": "not found"},
        )
    )

    payload = await agent_operations.agent_host_telemetry(
        "10.0.0.5",
        5100,
        http_client_factory=_strict_client_factory(client),
    )

    assert payload is None


async def test_appium_probe_session_success() -> None:
    client = StrictAgentClient(
        post_response=_response(
            "POST",
            "http://10.0.0.5:5100/agent/appium/4723/probe-session",
            payload={"ok": True},
        )
    )

    ok, detail = await agent_operations.appium_probe_session(
        "10.0.0.5",
        5100,
        4723,
        capabilities={"platformName": "Android"},
        timeout_sec=42,
        http_client_factory=_strict_client_factory(client),
    )

    assert (ok, detail) == (True, None)
    assert client.post_calls[0][1]["timeout"] == 42


async def test_appium_probe_session_reports_http_failure_detail() -> None:
    client = StrictAgentClient(
        post_response=_response(
            "POST",
            "http://10.0.0.5:5100/agent/appium/4723/probe-session",
            status_code=500,
            payload={"detail": "session creation failed"},
        )
    )

    ok, detail = await agent_operations.appium_probe_session(
        "10.0.0.5",
        5100,
        4723,
        capabilities={},
        timeout_sec=10,
        http_client_factory=_strict_client_factory(client),
    )

    assert ok is False
    assert detail == "session creation failed"


async def test_appium_probe_session_reports_invalid_payload() -> None:
    client = StrictAgentClient(
        post_response=_response(
            "POST",
            "http://10.0.0.5:5100/agent/appium/4723/probe-session",
            payload={"ok": False},
        )
    )

    ok, detail = await agent_operations.appium_probe_session(
        "10.0.0.5",
        5100,
        4723,
        capabilities={},
        timeout_sec=10,
        http_client_factory=_strict_client_factory(client),
    )

    assert (ok, detail) == (False, "Probe session returned an invalid payload")


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
        )


async def test_appium_logs_and_tool_endpoints_raise_for_invalid_payload() -> None:
    logs_client = StrictAgentClient(
        get_response=_response("GET", "http://10.0.0.5:5100/agent/appium/4723/logs", payload=["bad"])
    )
    tool_status_client = StrictAgentClient(
        get_response=_response("GET", "http://10.0.0.5:5100/agent/tools/status", payload=["bad"])
    )
    ensure_tools_client = StrictAgentClient(
        post_response=_response("POST", "http://10.0.0.5:5100/agent/tools/ensure", payload=["bad"])
    )

    with pytest.raises(AgentUnreachableError, match="fetch Appium logs failed"):
        await agent_operations.appium_logs(
            "10.0.0.5",
            5100,
            4723,
            lines=10,
            http_client_factory=_strict_client_factory(logs_client),
        )

    with pytest.raises(AgentUnreachableError, match="fetch tool status failed"):
        await agent_operations.get_tool_status(
            "10.0.0.5",
            5100,
            http_client_factory=_strict_client_factory(tool_status_client),
        )

    with pytest.raises(AgentUnreachableError, match="ensure tools failed"):
        await agent_operations.ensure_tools(
            "10.0.0.5",
            5100,
            appium_version=None,
            selenium_jar_version=None,
            http_client_factory=_strict_client_factory(ensure_tools_client),
        )


async def test_list_plugins_filters_non_dict_payload_entries() -> None:
    client = StrictAgentClient(
        get_response=_response(
            "GET",
            "http://10.0.0.5:5100/agent/plugins",
            payload=[{"name": "images"}, 123, {"name": "execute-driver"}],
        )
    )

    payload = await agent_operations.list_plugins(
        "10.0.0.5",
        5100,
        http_client_factory=_strict_client_factory(client),
    )

    assert payload == [{"name": "images"}, {"name": "execute-driver"}]


async def test_sync_plugins_endpoint_returns_valid_payload() -> None:
    sync_plugins_client = StrictAgentClient(
        post_response=_response("POST", "http://10.0.0.5:5100/agent/plugins/sync", payload={"installed": []})
    )

    assert (
        await agent_operations.sync_plugins(
            "10.0.0.5",
            5100,
            plugins=[],
            http_client_factory=_strict_client_factory(sync_plugins_client),
        )
    ) == {"installed": []}


async def test_sync_plugins_raises_for_invalid_payload() -> None:
    client = StrictAgentClient(
        post_response=_response(
            "POST",
            "http://10.0.0.5:5100/agent/plugins/sync",
            payload=["bad"],
        )
    )

    with pytest.raises(AgentUnreachableError, match="sync plugins failed"):
        await agent_operations.sync_plugins(
            "10.0.0.5",
            5100,
            plugins=[{"name": "images"}],
            http_client_factory=_strict_client_factory(client),
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
        )


def test_response_json_dict_returns_empty_dict_for_non_mapping_payload() -> None:
    response = _response("GET", "http://example.test", payload=["not", "a", "dict"])
    assert agent_operations.response_json_dict(response) == {}
