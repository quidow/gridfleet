import asyncio
from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol, cast
from unittest.mock import AsyncMock, patch

import pytest
from httpx2 import ASGITransport, AsyncClient

from agent_app.appium import appium_mgr
from agent_app.host.capabilities import CapabilitiesCache
from agent_app.lifespan import lifespan
from agent_app.main import app
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import HardwareTelemetry, HealthCheckResult, LifecycleActionResult
from agent_app.pack.dependencies import _latest_desired
from agent_app.registration import RegistrationService
from tests.pack.fake_worker import FakeWorkerHandle

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

    from agent_app.pack.manifest import DesiredPack


class _AdapterContext(Protocol):
    device_identity_value: object
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


async def test_lifespan_refreshes_and_cleans_up_background_tasks() -> None:
    stop_event = asyncio.Event()

    async def _wait_forever(*_args: object, **_kwargs: object) -> None:
        await stop_event.wait()

    with (
        patch.object(CapabilitiesCache, "refresh", new_callable=AsyncMock) as refresh,
        patch.object(CapabilitiesCache, "run_refresh_loop", side_effect=_wait_forever),
        patch.object(RegistrationService, "run", side_effect=_wait_forever),
        patch("agent_app.appium.appium_mgr.shutdown", new_callable=AsyncMock) as shutdown,
    ):
        async with lifespan(app):
            pass

    refresh.assert_awaited_once()
    shutdown.assert_awaited_once()
    stop_event.set()


class _FakeAdapter:
    pack_id = "appium-uiautomator2"
    pack_release = "1.0"

    def __init__(self) -> None:
        self.health_calls: list[str] = []
        self.telemetry_calls: list[tuple[str, str]] = []
        self.lifecycle_calls: list[tuple[str, str, dict[str, object]]] = []

    async def health_check(self, ctx: object) -> list[HealthCheckResult]:
        ctx_any = cast("_AdapterContext", ctx)
        self.health_calls.append(str(ctx_any.device_identity_value))
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


async def test_pack_device_health_endpoint_covers_forwarding(client: AsyncClient) -> None:
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
            "/agent/pack/devices/abc123/health",
            params={
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "device_type": "emulator",
            },
        )

    assert resp.status_code == 200
    assert adapter.health_calls == ["abc123"]


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
                identity_scheme="android_serial",
                identity_scope="host",
                stereotype={},
            )
        ],
    )
    adapter = _FakeAdapter()
    registry = AdapterRegistry()
    registry.set(desired_pack.id, desired_pack.release, FakeWorkerHandle(adapter))  # type: ignore[arg-type]
    app.state.adapter_registry = registry

    with _latest_desired_override(desired_pack):
        resp = await client.post(
            "/agent/pack/devices/device-1/lifecycle/reconnect",
            params={"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"},
            json={"ip_address": "192.168.1.10", "port": 5556},
        )

    assert resp.status_code == 200
    assert resp.json() == {"success": True, "detail": "", "state": "reconnecting"}
    assert adapter.lifecycle_calls == [("device-1", "reconnect", {"ip_address": "192.168.1.10", "port": 5556})]


async def test_appium_logs_caps_requested_lines(client: AsyncClient) -> None:
    # lines > 5000 is now rejected at the query boundary (Query le=5000) rather
    # than silently truncated at runtime, so expect 422 for out-of-range values.
    with patch("agent_app.appium.appium_mgr.get_logs", return_value=["line"]):
        resp = await client.get("/agent/appium/4723/logs", params={"lines": 9999})

    assert resp.status_code == 422


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
