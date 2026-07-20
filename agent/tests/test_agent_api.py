from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator  # noqa: TC003 - contextmanager signature is runtime-inspected
from contextlib import contextmanager
from typing import Protocol, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx2 import ASGITransport, AsyncClient

from agent_app.appium import appium_mgr
from agent_app.appium.dependencies import get_appium_mgr
from agent_app.host.dependencies import (
    get_capabilities_snapshot_dep,
    get_host_telemetry_dep,
    get_version_guidance_payload,
)
from agent_app.main import app
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import (
    HealthCheckResult,
    LifecycleActionResult,
)
from agent_app.pack.dependencies import _latest_desired
from agent_app.pack.manifest import DesiredPack  # noqa: TC001 - contextmanager signature is runtime-inspected
from agent_app.tools.dependencies import get_tool_status_dep
from tests.pack.fake_worker import FakeWorkerHandle


class _AdapterContext(Protocol):
    device_identity_value: object


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as c:
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
    assert data["appium_processes"] == {"running_nodes": [], "recent_restart_events": [], "start_failures": []}


async def test_host_telemetry(client: AsyncClient) -> None:
    async def _fake() -> dict[str, object]:
        return {
            "recorded_at": "2026-04-16T09:30:00+00:00",
            "cpu_percent": 71.2,
            "memory_used_mb": 24576,
            "memory_total_mb": 32768,
            "disk_used_gb": 412.3,
            "disk_total_gb": 512.0,
            "disk_percent": 80.5,
        }

    app.dependency_overrides[get_host_telemetry_dep] = _fake
    try:
        resp = await client.get("/agent/host/telemetry")
    finally:
        app.dependency_overrides.pop(get_host_telemetry_dep, None)

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
                identity_scheme="android_serial",
                identity_scope="host",
                stereotype={},
                appium_platform_name="Android",
            )
        ],
    )
    adapter = _FakeAdapter()
    registry = AdapterRegistry()
    registry.set(desired_pack.id, desired_pack.release, FakeWorkerHandle(adapter))  # type: ignore[arg-type]
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
    assert adapter.health_calls == ["serial-1"]


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
                identity_scheme="android_serial",
                identity_scope="host",
                stereotype={},
                appium_platform_name="Android",
            )
        ],
    )
    registry = AdapterRegistry()
    registry.set(desired_pack.id, desired_pack.release, FakeWorkerHandle(_FakeAdapter()))  # type: ignore[arg-type]
    app.state.adapter_registry = registry

    captured: dict[str, object] = {}

    async def fake_dispatch_health_check(handle: object, ctx: object) -> list[HealthCheckResult]:
        del handle
        captured["ctx"] = ctx
        return [HealthCheckResult(check_id="fake", ok=True)]

    with (
        _latest_desired_override(desired_pack),
        patch("agent_app.pack.router.dispatch_health_check", new=fake_dispatch_health_check),
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
    ctx = captured["ctx"]
    assert ctx.ip_address == "10.0.0.7"
    assert ctx.ip_ping_timeout_sec == 1.5
    assert ctx.ip_ping_count == 2


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
        self.health_calls: list[str] = []
        self.lifecycle_calls: list[tuple[str, str, dict[str, object]]] = []

    async def health_check(self, ctx: object) -> list[HealthCheckResult]:
        ctx_any = cast("_AdapterContext", ctx)
        self.health_calls.append(str(ctx_any.device_identity_value))
        return [HealthCheckResult(check_id="adapter_alive", ok=True)]

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
    registry.set(desired_pack.id, desired_pack.release, FakeWorkerHandle(adapter))  # type: ignore[arg-type]
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


async def test_pack_device_lifecycle_resolve_does_not_wake_health_probe(client: AsyncClient) -> None:
    desired_pack = _make_adb_desired_pack()
    adapter = _FakeAdapter()
    adapter.lifecycle_action = AsyncMock(
        return_value=LifecycleActionResult(
            ok=True,
            identity_value="avd:Pixel_6",
            connection_target="Pixel_6",
            resolved_connection_target="emulator-5554",
        )
    )
    registry = AdapterRegistry()
    registry.set(desired_pack.id, desired_pack.release, FakeWorkerHandle(adapter))  # type: ignore[arg-type]
    app.state.adapter_registry = registry
    probe_loop = MagicMock()
    previous_probe_loop = getattr(app.state, "probe_loop", None)
    app.state.probe_loop = probe_loop

    try:
        with _latest_desired_override(desired_pack):
            resp = await client.post(
                "/agent/pack/devices/device-1/lifecycle/resolve",
                params={"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"},
                json={},
            )
    finally:
        app.state.probe_loop = previous_probe_loop

    assert resp.status_code == 200
    assert resp.json() == {
        "success": True,
        "state": "",
        "detail": "",
        "identity_value": "avd:Pixel_6",
        "connection_target": "Pixel_6",
        "resolved_connection_target": "emulator-5554",
    }
    probe_loop.request_immediate.assert_not_called()


@pytest.mark.parametrize("action", ["unknown_action", "boot", "shutdown", "state"])
async def test_pack_device_lifecycle_unsupported_action(client: AsyncClient, action: str) -> None:
    desired_pack = _make_adb_desired_pack()
    adapter = _FakeAdapter()
    registry = AdapterRegistry()
    registry.set(desired_pack.id, desired_pack.release, FakeWorkerHandle(adapter))  # type: ignore[arg-type]
    app.state.adapter_registry = registry
    with _latest_desired_override(desired_pack):
        resp = await client.post(
            f"/agent/pack/devices/device-1/lifecycle/{action}",
            params={"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"},
            json={},
        )

    assert resp.status_code == 422
    assert adapter.lifecycle_calls == []


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


async def test_agent_tools_status(client: AsyncClient) -> None:
    async def _fake() -> dict[str, object]:
        return {
            "host": {
                "node": {"name": "Node", "version": "24.14.1", "description": "JavaScript runtime for Appium server"},
                "node_provider": {"name": "Node Provider", "version": "fnm", "description": "Node.js version manager"},
            },
            "packs": {
                "test-pack": [
                    {"name": "xcodebuild", "version": "16.0", "description": "Xcode builder"},
                ],
            },
        }

    app.dependency_overrides[get_tool_status_dep] = _fake
    try:
        resp = await client.get("/agent/tools/status")
    finally:
        app.dependency_overrides.pop(get_tool_status_dep, None)

    assert resp.status_code == 200
    assert resp.json()["host"]["node_provider"]["version"] == "fnm"
    assert resp.json()["packs"]["test-pack"][0]["version"] == "16.0"


async def test_agent_tools_ensure_route_removed(client: AsyncClient) -> None:
    resp = await client.post("/agent/tools/ensure", json={"appium_version": "3.3.0"})

    assert resp.status_code == 404


async def test_health_includes_version_guidance(client: AsyncClient) -> None:
    guidance = {
        "required_agent_version": "0.2.0",
        "recommended_agent_version": "0.3.0",
        "agent_version_status": "outdated",
        "agent_update_available": True,
    }
    app.dependency_overrides[get_version_guidance_payload] = lambda: guidance
    app.dependency_overrides[get_capabilities_snapshot_dep] = lambda: {"missing_prerequisites": []}
    try:
        resp = await client.get("/agent/health")
    finally:
        app.dependency_overrides.pop(get_version_guidance_payload, None)
        app.dependency_overrides.pop(get_capabilities_snapshot_dep, None)

    assert resp.status_code == 200
    assert resp.json()["version_guidance"] == guidance
