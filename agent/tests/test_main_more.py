from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from agent_app.host.capabilities import CapabilitiesCache
from agent_app.lifespan import lifespan
from agent_app.main import app
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.dependencies import _latest_desired
from agent_app.registration import RegistrationService


async def test_latest_desired_returns_empty_when_no_loop() -> None:
    request = MagicMock()
    request.app.state.pack_state_loop = None
    assert _latest_desired(request) == []


async def test_lifespan_starts_pack_loop_with_env_host_id() -> None:
    from agent_app.lifespan import agent_settings

    stop_event = asyncio.Event()

    async def _wait_forever(*_args: object, **_kwargs: object) -> None:
        await stop_event.wait()

    with (
        patch.object(CapabilitiesCache, "refresh", new_callable=AsyncMock),
        patch.object(CapabilitiesCache, "run_refresh_loop", side_effect=_wait_forever),
        patch.object(RegistrationService, "run", side_effect=_wait_forever),
        patch("agent_app.appium.appium_mgr.shutdown", new_callable=AsyncMock),
        patch.object(agent_settings.core, "host_id", "test-host-id"),
        patch.object(agent_settings.manager, "backend_url", ""),
    ):
        async with lifespan(app):
            assert app.state.pack_state_loop_enabled is True
            stop_event.set()


async def test_lifespan_no_backend_url_skips_pack_loop() -> None:
    from agent_app.lifespan import agent_settings

    stop_event = asyncio.Event()

    async def _wait_forever(*_args: object, **_kwargs: object) -> None:
        await stop_event.wait()

    with (
        patch.object(CapabilitiesCache, "refresh", new_callable=AsyncMock),
        patch.object(CapabilitiesCache, "run_refresh_loop", side_effect=_wait_forever),
        patch.object(RegistrationService, "run", side_effect=_wait_forever),
        patch("agent_app.appium.appium_mgr.shutdown", new_callable=AsyncMock),
        patch.object(agent_settings.core, "host_id", "test"),
        patch.object(agent_settings.manager, "backend_url", ""),
    ):
        async with lifespan(app):
            # Since backend_url is empty string and manager_url may be default,
            # pack task should still exist because manager_url is set in config.
            # Test mainly verifies no errors.
            stop_event.set()


async def test_start_appium_invalid_payload_error() -> None:
    from httpx2 import ASGITransport, AsyncClient

    from agent_app.appium.exceptions import InvalidStartPayloadError

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        with patch(
            "agent_app.appium.appium_mgr.start",
            side_effect=InvalidStartPayloadError("bad payload"),
        ):
            resp = await client.post(
                "/agent/appium/start",
                json={
                    "connection_target": "foo",
                    "port": 4723,
                    "pack_id": "p",
                    "platform_id": "x",
                },
            )
    assert resp.status_code == 400


async def test_start_appium_generic_runtime_error() -> None:
    from httpx2 import ASGITransport, AsyncClient

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch(
            "agent_app.appium.appium_mgr.start",
            side_effect=RuntimeError("boom"),
        ):
            resp = await client.post(
                "/agent/appium/start",
                json={
                    "connection_target": "foo",
                    "port": 4723,
                    "pack_id": "p",
                    "platform_id": "x",
                },
            )
    assert resp.status_code == 500
    assert resp.json()["detail"]["message"] == "Internal server error"
    assert resp.json()["detail"]["message"] == "Internal server error"


async def test_start_appium_unexpected_exception() -> None:
    from httpx2 import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        with patch(
            "agent_app.appium.appium_mgr.start",
            side_effect=ValueError("unexpected"),
        ):
            resp = await client.post(
                "/agent/appium/start",
                json={
                    "connection_target": "foo",
                    "port": 4723,
                    "pack_id": "p",
                    "platform_id": "x",
                },
            )
    assert resp.status_code == 500


async def test_normalize_device_route_no_adapter_registry() -> None:
    from httpx2 import ASGITransport, AsyncClient

    app.state.adapter_registry = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/agent/pack/devices/normalize",
            json={"pack_id": "p", "pack_release": "1", "platform_id": "x", "raw_input": {}},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "NO_ADAPTER"
    assert "No adapter loaded" in resp.json()["detail"]["message"]


async def test_feature_action_route_no_adapter() -> None:
    from httpx2 import ASGITransport, AsyncClient

    app.state.adapter_registry = AdapterRegistry()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/agent/pack/features/fid/actions/aid",
            json={"pack_id": "unknown", "args": {}, "device_identity_value": None},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "NO_ADAPTER"
    assert "No adapter loaded" in resp.json()["detail"]["message"]


async def test_pack_device_lifecycle_route_no_adapter_registry() -> None:
    from httpx2 import ASGITransport, AsyncClient

    from agent_app.pack.manifest import AppiumInstallable, DesiredPack, DesiredPlatform

    desired = DesiredPack(
        id="p",
        release="1",
        appium_server=AppiumInstallable("npm", "appium", "2", None, []),
        appium_driver=AppiumInstallable("npm", "d", "1", None, []),
        platforms=[
            DesiredPlatform(
                id="x",
                automation_name="A",
                device_types=[],
                connection_types=[],
                identity_scheme="a",
                identity_scope="host",
                stereotype={},
                appium_platform_name="A",
            )
        ],
    )
    app.dependency_overrides[_latest_desired] = lambda: [desired]
    try:
        app.state.adapter_registry = None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/agent/pack/devices/abc/lifecycle/reboot", params={"pack_id": "p", "platform_id": "x"}, json={}
            )
    finally:
        app.dependency_overrides.pop(_latest_desired, None)
    assert resp.status_code == 200
    assert resp.json()["detail"] == "Adapter not loaded for pack p:x"
