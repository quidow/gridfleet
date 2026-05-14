from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator  # noqa: TC003 - contextmanager signature is runtime-inspected
from contextlib import contextmanager
from typing import Protocol, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agent_app.appium import appium_mgr
from agent_app.appium.dependencies import get_appium_mgr
from agent_app.host.dependencies import get_capabilities_snapshot_dep
from agent_app.main import app
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import (
    HardwareTelemetry,
    HealthCheckResult,
    LifecycleActionResult,
)
from agent_app.pack.dependencies import _latest_desired
from agent_app.pack.manifest import DesiredPack  # noqa: TC001 - contextmanager signature is runtime-inspected
from agent_app.plugins.dependencies import get_installed_plugins_dep, sync_plugins_dep
from agent_app.tools.dependencies import get_tool_status_dep


class _AdapterContext(Protocol):
    device_identity_value: object
    allow_boot: object
    connection_target: object


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await appium_mgr.shutdown()


@contextmanager
def _latest_desired_override(*packs: DesiredPack) -> Iterator[None]:
    app.dependency_overrides[_latest_desired] = lambda: list(packs)
    try:
        yield
    finally:
        app.dependency_overrides.pop(_latest_desired, None)


async def test_health(client: AsyncClient) -> None:
    fake_caps = {"platforms": [], "tools": {}, "missing_prerequisites": []}
    app.dependency_overrides[get_capabilities_snapshot_dep] = lambda: fake_caps
    try:
        resp = await client.get("/agent/health")
    finally:
        app.dependency_overrides.pop(get_capabilities_snapshot_dep, None)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "hostname" in data
    assert "os_type" in data
    assert data["missing_prerequisites"] == []
    assert data["appium_processes"] == {"running_nodes": [], "recent_restart_events": []}


async def test_host_telemetry(client: AsyncClient) -> None:
    with patch(
        "agent_app.host.router.get_host_telemetry",
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

    with _latest_desired_override(desired_pack):
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
        _latest_desired_override(desired_pack),
        patch("agent_app.pack.router.adapter_health_check", new=fake_adapter_health_check),
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
    assert captured["ip_address"] == "10.0.0.7"
    assert captured["ip_ping_timeout_sec"] == 1.5
    assert captured["ip_ping_count"] == 2


async def test_pack_device_telemetry_dispatches_correctly(client: AsyncClient) -> None:
    desired_pack = _make_adb_desired_pack()
    adapter = _FakeAdapter()
    registry = AdapterRegistry()
    registry.set(desired_pack.id, desired_pack.release, adapter)  # type: ignore[arg-type]
    app.state.adapter_registry = registry

    with _latest_desired_override(desired_pack):
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
    with _latest_desired_override(desired_pack):
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

    with _latest_desired_override(desired_pack):
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
    with _latest_desired_override(desired_pack):
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
    fake_mgr = MagicMock()
    fake_mgr.start = AsyncMock(return_value=mock_info)
    app.dependency_overrides[get_appium_mgr] = lambda: fake_mgr
    try:
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
    finally:
        app.dependency_overrides.pop(get_appium_mgr, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["pid"] == 1234
    assert data["port"] == 4723
    fake_mgr.start.assert_awaited_once()
    assert fake_mgr.start.await_args is not None
    assert fake_mgr.start.await_args.kwargs["session_override"] is False
    assert fake_mgr.start.await_args.kwargs["pack_id"] == "appium-uiautomator2"
    assert fake_mgr.start.await_args.kwargs["platform_id"] == "android_mobile"


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
    fake_mgr = MagicMock()
    fake_mgr.start = AsyncMock(side_effect=RuntimeError("appium not found"))
    app.dependency_overrides[get_appium_mgr] = lambda: fake_mgr
    try:
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
    finally:
        app.dependency_overrides.pop(get_appium_mgr, None)

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["code"] == "INTERNAL_ERROR"
    assert "appium not found" in detail["message"]


async def test_stop_appium(client: AsyncClient) -> None:
    fake_mgr = MagicMock()
    fake_mgr.stop = AsyncMock(return_value=None)
    app.dependency_overrides[get_appium_mgr] = lambda: fake_mgr
    try:
        resp = await client.post("/agent/appium/stop", json={"port": 4723})
    finally:
        app.dependency_overrides.pop(get_appium_mgr, None)

    assert resp.status_code == 200
    assert resp.json()["stopped"] is True


async def test_appium_status(client: AsyncClient) -> None:
    fake_mgr = MagicMock()
    fake_mgr.status = AsyncMock(return_value={"running": True, "port": 4723})
    app.dependency_overrides[get_appium_mgr] = lambda: fake_mgr
    try:
        resp = await client.get("/agent/appium/4723/status")
    finally:
        app.dependency_overrides.pop(get_appium_mgr, None)

    assert resp.status_code == 200
    assert resp.json()["running"] is True


async def test_appium_logs(client: AsyncClient) -> None:
    fake_mgr = MagicMock()
    fake_mgr.get_logs = MagicMock(return_value=["line 1", "line 2"])
    app.dependency_overrides[get_appium_mgr] = lambda: fake_mgr
    try:
        resp = await client.get("/agent/appium/4723/logs", params={"lines": 2})
    finally:
        app.dependency_overrides.pop(get_appium_mgr, None)

    assert resp.status_code == 200
    assert resp.json() == {"port": 4723, "lines": ["line 1", "line 2"], "count": 2}
    fake_mgr.get_logs.assert_called_once_with(4723, lines=2)


async def test_probe_appium_session_route_is_not_available(client: AsyncClient) -> None:
    resp = await client.post("/agent/appium/4723/probe-session", json={"capabilities": {"platformName": "Android"}})

    assert resp.status_code == 404


async def test_list_plugins(client: AsyncClient) -> None:
    async def _fake() -> list[dict[str, str]]:
        return [{"name": "execute-driver", "version": "1.0.0"}]

    app.dependency_overrides[get_installed_plugins_dep] = _fake
    try:
        resp = await client.get("/agent/plugins")
    finally:
        app.dependency_overrides.pop(get_installed_plugins_dep, None)

    assert resp.status_code == 200
    assert resp.json() == [{"name": "execute-driver", "version": "1.0.0"}]


async def test_sync_plugins(client: AsyncClient) -> None:
    captured: list[list[dict[str, object]]] = []

    async def _fake_sync(configs: list[dict[str, object]]) -> dict[str, object]:
        captured.append(configs)
        return {"installed": ["execute-driver"], "updated": [], "removed": [], "errors": {}}

    app.dependency_overrides[sync_plugins_dep] = lambda: _fake_sync
    try:
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
    finally:
        app.dependency_overrides.pop(sync_plugins_dep, None)

    assert resp.status_code == 200
    assert resp.json()["installed"] == ["execute-driver"]
    assert captured == [
        [
            {
                "name": "execute-driver",
                "version": "1.0.0",
                "source": "npm:@appium/execute-driver-plugin",
                "package": None,
            }
        ]
    ]


async def test_agent_tools_status(client: AsyncClient) -> None:
    async def _fake() -> dict[str, object]:
        return {"node": "24.14.1", "node_provider": "fnm", "go_ios": "1.0.207"}

    app.dependency_overrides[get_tool_status_dep] = _fake
    try:
        resp = await client.get("/agent/tools/status")
    finally:
        app.dependency_overrides.pop(get_tool_status_dep, None)

    assert resp.status_code == 200
    assert resp.json()["node_provider"] == "fnm"
    assert resp.json()["go_ios"] == "1.0.207"


async def test_agent_tools_ensure_route_removed(client: AsyncClient) -> None:
    resp = await client.post("/agent/tools/ensure", json={"appium_version": "3.3.0"})

    assert resp.status_code == 404


async def test_health_includes_version_guidance(client: AsyncClient) -> None:
    from agent_app.host.version_guidance import clear_version_guidance, update_version_guidance

    clear_version_guidance()
    update_version_guidance(
        {
            "required_agent_version": "0.2.0",
            "recommended_agent_version": "0.3.0",
            "agent_version_status": "outdated",
            "agent_update_available": True,
        }
    )
    app.dependency_overrides[get_capabilities_snapshot_dep] = lambda: {"missing_prerequisites": []}
    try:
        resp = await client.get("/agent/health")
    finally:
        app.dependency_overrides.pop(get_capabilities_snapshot_dep, None)

    assert resp.status_code == 200
    assert resp.json()["version_guidance"] == {
        "required_agent_version": "0.2.0",
        "recommended_agent_version": "0.3.0",
        "agent_version_status": "outdated",
        "agent_update_available": True,
    }
    clear_version_guidance()
