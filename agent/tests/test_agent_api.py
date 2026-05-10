from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from httpx import Response as HttpxResponse

from agent_app.appium_process import DeviceNotFoundError
from agent_app.error_codes import AgentErrorCode
from agent_app.main import app, appium_mgr
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import (
    HardwareTelemetry,
    HealthCheckResult,
    LifecycleActionResult,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from agent_app.pack.manifest import DesiredPack


class _AdapterContext(Protocol):
    device_identity_value: object
    allow_boot: object
    connection_target: object


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await appium_mgr.shutdown()


async def test_health(client: AsyncClient) -> None:
    with patch(
        "agent_app.main.get_capabilities_snapshot",
        return_value={"platforms": [], "tools": {}, "missing_prerequisites": ["java"]},
    ):
        resp = await client.get("/agent/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "hostname" in data
    assert "os_type" in data
    assert data["missing_prerequisites"] == ["java"]
    assert data["appium_processes"] == {"running_nodes": [], "recent_restart_events": []}


async def test_host_telemetry(client: AsyncClient) -> None:
    with patch(
        "agent_app.main.get_host_telemetry",
        new_callable=AsyncMock,
        return_value={
            "recorded_at": "2026-04-16T09:30:00+00:00",
            "cpu_percent": 71.2,
            "memory_used_mb": 24576,
            "memory_total_mb": 32768,
            "disk_used_gb": 412.3,
            "disk_total_gb": 512.0,
            "disk_percent": 80.5,
        },
    ):
        resp = await client.get("/agent/host/telemetry")

    assert resp.status_code == 200
    data = resp.json()
    assert data["cpu_percent"] == 71.2
    assert data["memory_total_mb"] == 32768
    assert data["disk_percent"] == 80.5


async def test_pack_device_health_requires_query_params(client: AsyncClient) -> None:
    resp = await client.get("/agent/pack/devices/abc-123/health")
    assert resp.status_code == 422


async def test_pack_device_health_dispatches_correctly(client: AsyncClient) -> None:
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
            "/agent/pack/devices/serial-1/health",
            params={
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "device_type": "real_device",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["healthy"] is True
    assert adapter.health_calls == [("serial-1", False)]


async def test_pack_device_health_forwards_ip_ping_params(client: AsyncClient) -> None:
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
                appium_platform_name="Android",
            )
        ],
    )
    registry = AdapterRegistry()
    registry.set(desired_pack.id, desired_pack.release, _FakeAdapter())  # type: ignore[arg-type]
    app.state.adapter_registry = registry

    captured: dict[str, object] = {}

    async def fake_adapter_health_check(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"healthy": True, "checks": []}

    with (
        patch("agent_app.main._latest_desired", return_value=[desired_pack]),
        patch("agent_app.main.adapter_health_check", new=fake_adapter_health_check),
    ):
        resp = await client.get(
            "/agent/pack/devices/abc/health",
            params={
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "device_type": "real_device",
                "connection_type": "usb",
                "ip_address": "10.0.0.7",
                "ip_ping_timeout_sec": 1.5,
                "ip_ping_count": 2,
            },
        )

    assert resp.status_code == 200
    assert captured["ip_ping_timeout_sec"] == 1.5
    assert captured["ip_ping_count"] == 2


async def test_pack_device_telemetry_dispatches_correctly(client: AsyncClient) -> None:
    desired_pack = _make_adb_desired_pack()
    adapter = _FakeAdapter()
    registry = AdapterRegistry()
    registry.set(desired_pack.id, desired_pack.release, adapter)  # type: ignore[arg-type]
    app.state.adapter_registry = registry

    with patch("agent_app.main._latest_desired", return_value=[desired_pack]):
        resp = await client.get(
            "/agent/pack/devices/serial-1/telemetry",
            params={
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "device_type": "real_device",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["battery_level_percent"] == 84
    assert adapter.telemetry_calls == [("serial-1", "serial-1")]


async def test_pack_device_telemetry_returns_404_when_none(client: AsyncClient) -> None:
    desired_pack = _make_adb_desired_pack()
    app.state.adapter_registry = AdapterRegistry()
    with patch("agent_app.main._latest_desired", return_value=[desired_pack]):
        resp = await client.get(
            "/agent/pack/devices/missing/telemetry",
            params={
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "device_type": "real_device",
            },
        )

    assert resp.status_code == 404


def _make_adb_desired_pack() -> DesiredPack:
    from agent_app.pack.manifest import AppiumInstallable, DesiredPack, DesiredPlatform

    return DesiredPack(
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
                appium_platform_name="Android",
            )
        ],
    )


class _FakeAdapter:
    pack_id = "appium-uiautomator2"
    pack_release = "1.0"

    def __init__(self) -> None:
        self.health_calls: list[tuple[str, bool]] = []
        self.telemetry_calls: list[tuple[str, str]] = []
        self.lifecycle_calls: list[tuple[str, str, dict[str, object]]] = []

    async def health_check(self, ctx: object) -> list[HealthCheckResult]:
        ctx_any = cast("_AdapterContext", ctx)
        self.health_calls.append((str(ctx_any.device_identity_value), bool(ctx_any.allow_boot)))
        return [HealthCheckResult(check_id="adapter_alive", ok=True)]

    async def telemetry(self, ctx: object) -> HardwareTelemetry:
        ctx_any = cast("_AdapterContext", ctx)
        self.telemetry_calls.append((str(ctx_any.device_identity_value), str(ctx_any.connection_target)))
        return HardwareTelemetry(supported=True, battery_level_percent=84)

    async def lifecycle_action(
        self,
        action_id: str,
        args: dict[str, object],
        ctx: object,
    ) -> LifecycleActionResult:
        ctx_any = cast("_AdapterContext", ctx)
        self.lifecycle_calls.append((str(ctx_any.device_identity_value), action_id, args))
        return LifecycleActionResult(ok=True, state="reconnecting")


async def test_pack_device_lifecycle_reconnect(client: AsyncClient) -> None:
    desired_pack = _make_adb_desired_pack()
    adapter = _FakeAdapter()
    registry = AdapterRegistry()
    registry.set(desired_pack.id, desired_pack.release, adapter)  # type: ignore[arg-type]
    app.state.adapter_registry = registry

    with patch("agent_app.main._latest_desired", return_value=[desired_pack]):
        resp = await client.post(
            "/agent/pack/devices/device-1/lifecycle/reconnect",
            params={"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"},
            json={"ip_address": "192.168.1.10", "port": 5555},
        )

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert adapter.lifecycle_calls == [("device-1", "reconnect", {"ip_address": "192.168.1.10", "port": 5555})]


async def test_pack_device_lifecycle_unsupported_action(client: AsyncClient) -> None:
    desired_pack = _make_adb_desired_pack()
    app.state.adapter_registry = AdapterRegistry()
    with patch("agent_app.main._latest_desired", return_value=[desired_pack]):
        resp = await client.post(
            "/agent/pack/devices/device-1/lifecycle/unknown_action",
            params={"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"},
            json={},
        )

    assert resp.status_code == 200
    assert "Adapter not loaded" in resp.json()["detail"]


async def test_normalize_device_no_adapter(client: AsyncClient) -> None:
    resp = await client.post(
        "/agent/pack/devices/normalize",
        json={
            "pack_id": "unknown",
            "pack_release": "0.0.0",
            "platform_id": "android_mobile",
            "raw_input": {"connection_target": "ABC123"},
        },
    )
    assert resp.status_code == 404


async def test_start_appium(client: AsyncClient) -> None:
    mock_info = MagicMock(pid=1234, port=4723, connection_target="abc-123")

    with patch("agent_app.main.appium_mgr.start", new_callable=AsyncMock, return_value=mock_info) as start_mock:
        resp = await client.post(
            "/agent/appium/start",
            json={
                "connection_target": "abc-123",
                "port": 4723,
                "grid_url": "http://grid:4444",
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "session_override": False,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["pid"] == 1234
    assert data["port"] == 4723
    start_mock.assert_awaited_once()
    assert start_mock.await_args is not None
    assert start_mock.await_args.kwargs["session_override"] is False
    assert start_mock.await_args.kwargs["pack_id"] == "appium-uiautomator2"
    assert start_mock.await_args.kwargs["platform_id"] == "android_mobile"


async def test_start_appium_requires_pack_identity(client: AsyncClient) -> None:
    resp = await client.post(
        "/agent/appium/start",
        json={
            "connection_target": "abc-123",
            "port": 4723,
            "grid_url": "http://grid:4444",
            "platform_id": "android_mobile",
        },
    )

    assert resp.status_code == 422


async def test_start_appium_failure(client: AsyncClient) -> None:
    with patch("agent_app.main.appium_mgr.start", new_callable=AsyncMock, side_effect=RuntimeError("appium not found")):
        resp = await client.post(
            "/agent/appium/start",
            json={
                "connection_target": "abc-123",
                "port": 4723,
                "grid_url": "http://grid:4444",
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
            },
        )

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["code"] == "INTERNAL_ERROR"
    assert "appium not found" in detail["message"]


async def test_stop_appium(client: AsyncClient) -> None:
    with patch("agent_app.main.appium_mgr.stop", new_callable=AsyncMock):
        resp = await client.post("/agent/appium/stop", json={"port": 4723})

    assert resp.status_code == 200
    assert resp.json()["stopped"] is True


async def test_appium_status(client: AsyncClient) -> None:
    with patch(
        "agent_app.main.appium_mgr.status", new_callable=AsyncMock, return_value={"running": True, "port": 4723}
    ):
        resp = await client.get("/agent/appium/4723/status")

    assert resp.status_code == 200
    assert resp.json()["running"] is True


async def test_appium_logs(client: AsyncClient) -> None:
    with patch("agent_app.main.appium_mgr.get_logs", return_value=["line 1", "line 2"]) as get_logs:
        resp = await client.get("/agent/appium/4723/logs", params={"lines": 2})

    assert resp.status_code == 200
    assert resp.json() == {"port": 4723, "lines": ["line 1", "line 2"], "count": 2}
    get_logs.assert_called_once_with(4723, lines=2)


async def test_probe_appium_session(client: AsyncClient) -> None:
    create_response = MagicMock(spec=HttpxResponse, status_code=200)
    create_response.json.return_value = {"value": {"sessionId": "session-123"}}
    delete_response = MagicMock(spec=HttpxResponse, status_code=200)

    mock_http_client = MagicMock()
    mock_http_client.__aenter__.return_value = mock_http_client
    mock_http_client.__aexit__.return_value = False
    mock_http_client.post = AsyncMock(return_value=create_response)
    mock_http_client.delete = AsyncMock(return_value=delete_response)

    with (
        patch.object(appium_mgr, "require_managed_running_port"),
        patch("agent_app.main.httpx.AsyncClient", return_value=mock_http_client),
    ):
        resp = await client.post("/agent/appium/4723/probe-session", json={"capabilities": {"platformName": "Android"}})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    mock_http_client.post.assert_awaited_once_with(
        "/session",
        json={"capabilities": {"alwaysMatch": {"platformName": "Android"}, "firstMatch": [{}]}},
    )


async def test_probe_appium_session_maps_managed_port_race_to_not_found(client: AsyncClient) -> None:
    with patch.object(
        appium_mgr,
        "loopback_origin_for_managed_port",
        side_effect=DeviceNotFoundError("No managed Appium process is running on port 4723"),
    ):
        resp = await client.post("/agent/appium/4723/probe-session", json={"capabilities": {"platformName": "Android"}})

    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["code"] == AgentErrorCode.DEVICE_NOT_FOUND


async def test_probe_appium_session_strips_gridfleet_routing_metadata(client: AsyncClient) -> None:
    create_response = MagicMock(spec=HttpxResponse, status_code=200)
    create_response.json.return_value = {"value": {"sessionId": "session-123"}}
    delete_response = MagicMock(spec=HttpxResponse, status_code=200)

    mock_http_client = MagicMock()
    mock_http_client.__aenter__.return_value = mock_http_client
    mock_http_client.__aexit__.return_value = False
    mock_http_client.post = AsyncMock(return_value=create_response)
    mock_http_client.delete = AsyncMock(return_value=delete_response)

    capabilities = {
        "platformName": "roku",
        "appium:automationName": "Roku",
        "appium:ip": "192.168.1.2",
        "appium:password": "secret",
        "appium:udid": "192.168.1.2",
        "gridfleet:probeSession": True,
        "gridfleet:testName": "__gridfleet_probe__",
        "appium:gridfleet:deviceId": "device-1",
        "appium:gridfleet:deviceName": "Roku Stick",
        "appium:deviceName": "Roku Stick",
        "appium:platform": "roku_network",
        "appium:device_type": "real_device",
        "appium:os_version": "15.1.4",
        "appium:manufacturer": "Roku",
        "appium:model": "Streaming Stick 4K",
    }

    with (
        patch.object(appium_mgr, "require_managed_running_port"),
        patch("agent_app.main.httpx.AsyncClient", return_value=mock_http_client),
    ):
        resp = await client.post("/agent/appium/4723/probe-session", json={"capabilities": capabilities})

    assert resp.status_code == 200
    mock_http_client.post.assert_awaited_once_with(
        "/session",
        json={
            "capabilities": {
                "alwaysMatch": {
                    "platformName": "roku",
                    "appium:automationName": "Roku",
                    "appium:ip": "192.168.1.2",
                    "appium:password": "secret",
                    "appium:udid": "192.168.1.2",
                },
                "firstMatch": [{}],
            }
        },
    )


async def test_probe_appium_session_returns_gateway_error_on_cleanup_failure(client: AsyncClient) -> None:
    create_response = MagicMock(spec=HttpxResponse, status_code=200)
    create_response.json.return_value = {"value": {"sessionId": "session-123"}}
    delete_response = MagicMock(spec=HttpxResponse, status_code=500)
    delete_response.json.return_value = {"value": {"message": "delete failed"}}

    mock_http_client = MagicMock()
    mock_http_client.__aenter__.return_value = mock_http_client
    mock_http_client.__aexit__.return_value = False
    mock_http_client.post = AsyncMock(return_value=create_response)
    mock_http_client.delete = AsyncMock(return_value=delete_response)

    with (
        patch.object(appium_mgr, "require_managed_running_port"),
        patch("agent_app.main.httpx.AsyncClient", return_value=mock_http_client),
    ):
        resp = await client.post("/agent/appium/4723/probe-session", json={"capabilities": {"platformName": "Android"}})

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["code"] == "PROBE_FAILED"
    assert detail["message"] == "delete failed"


async def test_probe_appium_session_returns_timeout_status(client: AsyncClient) -> None:
    mock_http_client = MagicMock()
    mock_http_client.__aenter__.return_value = mock_http_client
    mock_http_client.__aexit__.return_value = False
    mock_http_client.post = AsyncMock(side_effect=httpx.ReadTimeout("slow", request=MagicMock()))

    with (
        patch.object(appium_mgr, "require_managed_running_port"),
        patch("agent_app.main.httpx.AsyncClient", return_value=mock_http_client),
    ):
        resp = await client.post("/agent/appium/4723/probe-session", json={"capabilities": {"platformName": "Android"}})

    assert resp.status_code == 504
    detail = resp.json()["detail"]
    assert detail["code"] == "PROBE_FAILED"
    assert "timed out" in detail["message"]


async def test_list_plugins(client: AsyncClient) -> None:
    with patch(
        "agent_app.main.get_installed_plugins",
        new_callable=AsyncMock,
        return_value=[{"name": "execute-driver", "version": "1.0.0"}],
    ):
        resp = await client.get("/agent/plugins")

    assert resp.status_code == 200
    assert resp.json() == [{"name": "execute-driver", "version": "1.0.0"}]


async def test_sync_plugins(client: AsyncClient) -> None:
    with patch(
        "agent_app.main.sync_plugins",
        new_callable=AsyncMock,
        return_value={"installed": ["execute-driver"], "updated": [], "removed": [], "errors": {}},
    ) as sync:
        resp = await client.post(
            "/agent/plugins/sync",
            json={
                "plugins": [
                    {
                        "name": "execute-driver",
                        "version": "1.0.0",
                        "source": "npm:@appium/execute-driver-plugin",
                    }
                ]
            },
        )

    assert resp.status_code == 200
    assert resp.json()["installed"] == ["execute-driver"]
    sync.assert_awaited_once_with(
        [
            {
                "name": "execute-driver",
                "version": "1.0.0",
                "source": "npm:@appium/execute-driver-plugin",
                "package": None,
            }
        ]
    )


async def test_agent_tools_status(client: AsyncClient) -> None:
    with patch(
        "agent_app.main.get_tool_status",
        new_callable=AsyncMock,
        return_value={
            "appium": "3.3.0",
            "node": "24.14.1",
            "node_provider": "fnm",
            "go_ios": "1.0.207",
            "selenium_jar": "4.41.0",
            "selenium_jar_path": "/opt/gridfleet-agent/selenium-server.jar",
        },
    ) as status:
        resp = await client.get("/agent/tools/status")

    assert resp.status_code == 200
    assert resp.json()["node_provider"] == "fnm"
    assert resp.json()["go_ios"] == "1.0.207"
    status.assert_awaited_once_with()


async def test_agent_tools_ensure_serializes_and_returns_result(client: AsyncClient) -> None:
    with patch(
        "agent_app.main.ensure_tools",
        new_callable=AsyncMock,
        return_value={"appium": {"success": True, "action": "none", "version": "3.3.0"}},
    ) as ensure:
        resp = await client.post(
            "/agent/tools/ensure",
            json={"appium_version": "3.3.0", "selenium_jar_version": None},
        )

    assert resp.status_code == 200
    assert resp.json()["appium"]["success"] is True
    ensure.assert_awaited_once_with("3.3.0", None)


async def test_health_includes_version_guidance(client: AsyncClient) -> None:
    from agent_app.version_guidance import clear_version_guidance, update_version_guidance

    clear_version_guidance()
    update_version_guidance(
        {
            "required_agent_version": "0.2.0",
            "recommended_agent_version": "0.3.0",
            "agent_version_status": "outdated",
            "agent_update_available": True,
        }
    )
    with patch("agent_app.main.get_capabilities_snapshot", return_value={"missing_prerequisites": []}):
        resp = await client.get("/agent/health")

    assert resp.status_code == 200
    assert resp.json()["version_guidance"] == {
        "required_agent_version": "0.2.0",
        "recommended_agent_version": "0.3.0",
        "agent_version_status": "outdated",
        "agent_update_available": True,
    }
    clear_version_guidance()


@pytest.mark.asyncio
async def test_probe_appium_session_rejects_unmanaged_port(client: AsyncClient) -> None:
    resp = await client.post("/agent/appium/6553/probe-session", json={"capabilities": {"platformName": "Android"}})

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == AgentErrorCode.DEVICE_NOT_FOUND
