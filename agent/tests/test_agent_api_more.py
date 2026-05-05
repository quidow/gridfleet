import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from httpx import Response as HttpxResponse

from agent_app.main import (
    _get_network_devices,
    _probe_failure_detail,
    app,
    appium_mgr,
    lifespan,
)
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import HardwareTelemetry, HealthCheckResult, LifecycleActionResult


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await appium_mgr.shutdown()


def test_get_network_devices_filters_non_network_targets() -> None:
    infos = [
        SimpleNamespace(connection_target="192.168.1.10:5555"),
        SimpleNamespace(connection_target="emulator-5554"),
    ]

    with patch("agent_app.main.appium_mgr.list_running", return_value=infos):
        assert _get_network_devices() == [
            {"connection_target": "192.168.1.10:5555", "ip_address": "192.168.1.10", "port": 5555}
        ]


async def test_lifespan_refreshes_and_cleans_up_background_tasks() -> None:
    stop_event = asyncio.Event()

    async def _wait_forever(*_args: object, **_kwargs: object) -> None:
        await stop_event.wait()

    with (
        patch("agent_app.main.refresh_capabilities_snapshot", new_callable=AsyncMock) as refresh,
        patch("agent_app.main.capabilities_refresh_loop", side_effect=_wait_forever),
        patch("agent_app.registration.registration_loop", side_effect=_wait_forever),
        patch("agent_app.main.appium_mgr.shutdown", new_callable=AsyncMock) as shutdown,
    ):
        async with lifespan(app):
            pass

    refresh.assert_awaited_once()
    shutdown.assert_awaited_once()
    stop_event.set()


def test_probe_failure_detail_prefers_rich_payloads_and_fallbacks() -> None:
    response = MagicMock(spec=HttpxResponse)
    response.json.return_value = {"value": {"message": "nested message"}}
    assert _probe_failure_detail(response, fallback="fallback") == "nested message"

    response.json.return_value = {"value": {"error": "nested error"}}
    assert _probe_failure_detail(response, fallback="fallback") == "nested error"

    response.json.return_value = {"detail": "detail text"}
    assert _probe_failure_detail(response, fallback="fallback") == "detail text"

    response.json.return_value = {"message": "message text"}
    assert _probe_failure_detail(response, fallback="fallback") == "message text"

    response.json.return_value = {"unexpected": True}
    assert _probe_failure_detail(response, fallback="fallback") == "fallback"

    response.json.side_effect = ValueError
    response.text = "plain text"
    assert _probe_failure_detail(response, fallback="fallback") == "plain text"


class _FakeAdapter:
    pack_id = "appium-uiautomator2"
    pack_release = "1.0"

    def __init__(self) -> None:
        self.health_calls: list[tuple[str, bool]] = []
        self.telemetry_calls: list[tuple[str, str]] = []
        self.lifecycle_calls: list[tuple[str, str, dict[str, object]]] = []

    async def health_check(self, ctx: object) -> list[HealthCheckResult]:
        ctx_any = cast("Any", ctx)
        self.health_calls.append((str(ctx_any.device_identity_value), bool(ctx_any.allow_boot)))
        return [HealthCheckResult(check_id="adapter_alive", ok=True)]

    async def telemetry(self, ctx: object) -> HardwareTelemetry:
        ctx_any = cast("Any", ctx)
        self.telemetry_calls.append((str(ctx_any.device_identity_value), str(ctx_any.connection_target)))
        return HardwareTelemetry(supported=True, battery_level_percent=84)

    async def lifecycle_action(
        self,
        action_id: str,
        args: dict[str, object],
        ctx: object,
    ) -> LifecycleActionResult:
        ctx_any = cast("Any", ctx)
        self.lifecycle_calls.append((str(ctx_any.device_identity_value), action_id, args))
        return LifecycleActionResult(ok=True, state="reconnecting")


async def test_pack_device_health_and_telemetry_endpoints_cover_forwarding_and_404(client: AsyncClient) -> None:
    from agent_app.pack.manifest import AppiumInstallable, DesiredPack, DesiredPlatform

    desired_pack = DesiredPack(
        id="appium-uiautomator2",
        release="1.0",
        appium_server=AppiumInstallable("npm", "appium", "2.11.5", None, []),
        appium_driver=AppiumInstallable("npm", "pkg", "1.0", None, []),
        platforms=[
            DesiredPlatform(
                id="android_mobile",
                automation_name="UiAutomator2",
                device_types=["emulator"],
                connection_types=[],
                grid_slots=["native"],
                identity_scheme="android_serial",
                identity_scope="host",
                stereotype={},
                appium_platform_name="Android",
            )
        ],
    )
    adapter = _FakeAdapter()
    registry = AdapterRegistry()
    registry.set(desired_pack.id, desired_pack.release, adapter)  # type: ignore[arg-type]
    app.state.adapter_registry = registry

    with patch("agent_app.main._latest_desired", return_value=[desired_pack]):
        resp = await client.get(
            "/agent/pack/devices/abc123/health",
            params={
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "device_type": "emulator",
                "allow_boot": "true",
                "headless": "false",
            },
        )

    assert resp.status_code == 200
    assert adapter.health_calls == [("abc123", True)]

    with patch("agent_app.main._latest_desired", return_value=[desired_pack]):
        resp = await client.get(
            "/agent/pack/devices/abc123/telemetry",
            params={
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "device_type": "emulator",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["battery_level_percent"] == 84
    assert adapter.telemetry_calls == [("abc123", "abc123")]

    app.state.adapter_registry = AdapterRegistry()
    with patch("agent_app.main._latest_desired", return_value=[desired_pack]):
        missing_resp = await client.get(
            "/agent/pack/devices/missing-device/telemetry",
            params={
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "device_type": "emulator",
            },
        )

    assert missing_resp.status_code == 404


async def test_pack_lifecycle_reconnect_endpoint(client: AsyncClient) -> None:
    from agent_app.pack.manifest import AppiumInstallable, DesiredPack, DesiredPlatform

    desired_pack = DesiredPack(
        id="appium-uiautomator2",
        release="1.0",
        appium_server=AppiumInstallable("npm", "appium", "2.11.5", None, []),
        appium_driver=AppiumInstallable("npm", "pkg", "1.0", None, []),
        platforms=[
            DesiredPlatform(
                id="android_mobile",
                automation_name="UiAutomator2",
                device_types=["real_device"],
                connection_types=["usb"],
                grid_slots=["native"],
                identity_scheme="android_serial",
                identity_scope="host",
                stereotype={},
            )
        ],
    )
    adapter = _FakeAdapter()
    registry = AdapterRegistry()
    registry.set(desired_pack.id, desired_pack.release, adapter)  # type: ignore[arg-type]
    app.state.adapter_registry = registry

    with patch("agent_app.main._latest_desired", return_value=[desired_pack]):
        resp = await client.post(
            "/agent/pack/devices/device-1/lifecycle/reconnect",
            params={"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"},
            json={"ip_address": "192.168.1.10", "port": 5556},
        )

    assert resp.status_code == 200
    assert resp.json() == {"success": True, "state": "reconnecting", "detail": ""}
    assert adapter.lifecycle_calls == [("device-1", "reconnect", {"ip_address": "192.168.1.10", "port": 5556})]


async def test_probe_session_covers_remaining_error_paths(client: AsyncClient) -> None:
    create_response = MagicMock(spec=HttpxResponse, status_code=400)
    create_response.json.return_value = {"detail": "bad create"}

    mock_http_client = MagicMock()
    mock_http_client.__aenter__.return_value = mock_http_client
    mock_http_client.__aexit__.return_value = False
    mock_http_client.post = AsyncMock(return_value=create_response)

    with patch("agent_app.main.httpx.AsyncClient", return_value=mock_http_client):
        resp = await client.post("/agent/appium/4723/probe-session", json={"capabilities": {"platformName": "Android"}})

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["code"] == "PROBE_FAILED"
    assert detail["message"] == "bad create"

    create_response = MagicMock(spec=HttpxResponse, status_code=200)
    create_response.json.side_effect = ValueError
    mock_http_client.post = AsyncMock(return_value=create_response)
    with patch("agent_app.main.httpx.AsyncClient", return_value=mock_http_client):
        resp = await client.post("/agent/appium/4723/probe-session", json={"capabilities": {"platformName": "Android"}})
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["code"] == "PROBE_FAILED"
    assert "invalid JSON" in detail["message"]

    create_response = MagicMock(spec=HttpxResponse, status_code=200)
    create_response.json.return_value = {"value": {}}
    mock_http_client.post = AsyncMock(return_value=create_response)
    with patch("agent_app.main.httpx.AsyncClient", return_value=mock_http_client):
        resp = await client.post("/agent/appium/4723/probe-session", json={"capabilities": {"platformName": "Android"}})
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["code"] == "PROBE_FAILED"
    assert "session id" in detail["message"]

    create_response = MagicMock(spec=HttpxResponse, status_code=200)
    create_response.json.return_value = {"sessionId": "session-123"}
    mock_http_client.post = AsyncMock(return_value=create_response)
    mock_http_client.delete = AsyncMock(side_effect=httpx.ReadTimeout("slow", request=MagicMock()))
    with patch("agent_app.main.httpx.AsyncClient", return_value=mock_http_client):
        resp = await client.post("/agent/appium/4723/probe-session", json={"capabilities": {"platformName": "Android"}})
    assert resp.status_code == 504
    detail = resp.json()["detail"]
    assert detail["code"] == "PROBE_FAILED"
    assert "cleanup timed out" in detail["message"]

    mock_http_client.delete = AsyncMock(side_effect=httpx.HTTPError("cleanup boom"))
    with patch("agent_app.main.httpx.AsyncClient", return_value=mock_http_client):
        resp = await client.post("/agent/appium/4723/probe-session", json={"capabilities": {"platformName": "Android"}})
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["code"] == "PROBE_FAILED"
    assert "cleanup failed" in detail["message"]


async def test_appium_logs_caps_requested_lines(client: AsyncClient) -> None:
    with patch("agent_app.main.appium_mgr.get_logs", return_value=["line"]) as get_logs:
        resp = await client.get("/agent/appium/4723/logs", params={"lines": 9999})

    assert resp.status_code == 200
    get_logs.assert_called_once_with(4723, lines=5000)


@pytest.mark.asyncio
async def test_legacy_agent_devices_endpoint_removed() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/agent/devices")
    assert resp.status_code in (404, 405)


@pytest.mark.asyncio
async def test_legacy_agent_device_health_endpoint_removed() -> None:
    params = {"platform": "android_mobile", "device_type": "real_device"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/agent/devices/serial-1/health", params=params)
    assert resp.status_code in (404, 405)


@pytest.mark.asyncio
async def test_legacy_agent_device_telemetry_endpoint_removed() -> None:
    params = {"platform": "android_mobile", "device_type": "real_device"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/agent/devices/serial-1/telemetry", params=params)
    assert resp.status_code in (404, 405)


@pytest.mark.asyncio
async def test_legacy_agent_reconnect_endpoint_removed() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/agent/devices/serial-1/reconnect", params={"ip_address": "10.0.0.1"})
    assert resp.status_code in (404, 405)


@pytest.mark.asyncio
async def test_legacy_android_resolve_endpoint_removed() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/agent/android/network-target/resolve", json={"connection_target": "10.0.0.1:5555"})
    assert resp.status_code in (404, 405)
