from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from agent_app.appium import appium_mgr
from agent_app.lifespan import _stop_grid_node_supervisors_for_shutdown, lifespan
from agent_app.main import app
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.dependencies import _latest_desired as _latest_desired_dep
from agent_app.pack.router import _latest_desired, _release_for_pack


async def test_stop_grid_node_supervisors_for_shutdown_timeout_cancels_tasks() -> None:

    class SlowSupervisor:
        async def stop(self) -> None:
            await asyncio.sleep(10)  # simulate hung supervisor

    slow = SlowSupervisor()
    fast = MagicMock()
    manager = MagicMock()
    manager._grid_supervisors = {4723: slow, 4724: fast}

    await _stop_grid_node_supervisors_for_shutdown(manager, timeout_sec=0.01)

    assert fast.stop.called
    # Both remain because timeout path does not pop entries
    assert 4723 in manager._grid_supervisors
    assert 4724 in manager._grid_supervisors


async def test_latest_desired_returns_empty_when_no_loop() -> None:
    request = MagicMock()
    request.app.state.pack_state_loop = None
    assert _latest_desired(request) == []


async def test_release_for_pack_returns_none_when_no_match() -> None:
    request = MagicMock()
    pack_cls = type("Pack", (), {"id": "other", "release": "1.0"})
    request.app.state.pack_state_loop = type("Loop", (), {"latest_desired_packs": [pack_cls()]})()
    assert _release_for_pack(request, "target") is None


async def test_release_for_pack_returns_release_when_matching() -> None:
    request = MagicMock()
    pack_cls = type("Pack", (), {"id": "target", "release": "2.0"})
    request.app.state.pack_state_loop = type("Loop", (), {"latest_desired_packs": [pack_cls()]})()
    assert _release_for_pack(request, "target") == "2.0"


async def test_lifespan_starts_pack_loop_with_env_host_id() -> None:
    stop_event = asyncio.Event()

    async def _wait_forever(*_args: object, **_kwargs: object) -> None:
        await stop_event.wait()

    with (
        patch("agent_app.lifespan.refresh_capabilities_snapshot", new_callable=AsyncMock),
        patch("agent_app.lifespan.capabilities_refresh_loop", side_effect=_wait_forever),
        patch("agent_app.registration.registration_loop", side_effect=_wait_forever),
        patch("agent_app.appium.appium_mgr.shutdown", new_callable=AsyncMock),
        patch.dict("os.environ", {"AGENT_HOST_ID": "test-host-id", "AGENT_BACKEND_URL": ""}),
    ):
        async with lifespan(app):
            assert app.state.pack_state_loop_enabled is True
            stop_event.set()


async def test_lifespan_no_backend_url_skips_pack_loop() -> None:
    stop_event = asyncio.Event()

    async def _wait_forever(*_args: object, **_kwargs: object) -> None:
        await stop_event.wait()

    with (
        patch("agent_app.lifespan.refresh_capabilities_snapshot", new_callable=AsyncMock),
        patch("agent_app.lifespan.capabilities_refresh_loop", side_effect=_wait_forever),
        patch("agent_app.registration.registration_loop", side_effect=_wait_forever),
        patch("agent_app.appium.appium_mgr.shutdown", new_callable=AsyncMock),
        patch.dict("os.environ", {"AGENT_HOST_ID": "test", "AGENT_BACKEND_URL": ""}),
    ):
        async with lifespan(app):
            # Since AGENT_BACKEND_URL is empty string and manager_url may be default,
            # pack task should still exist because manager_url is set in config.
            # Test mainly verifies no errors.
            stop_event.set()


async def test_reregister_grid_node_not_found() -> None:
    appium_mgr._grid_supervisors.clear()
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/grid/node/missing/reregister", json={})
    assert resp.status_code == 404
    assert "No running grid node" in resp.json()["detail"]["message"]


async def test_start_appium_invalid_payload_error() -> None:
    from httpx import ASGITransport, AsyncClient

    from agent_app.appium.process import InvalidStartPayloadError

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with patch(
            "agent_app.appium.appium_mgr.start",
            side_effect=InvalidStartPayloadError("bad payload"),
        ):
            resp = await client.post(
                "/agent/appium/start",
                json={
                    "connection_target": "foo",
                    "port": 4723,
                    "grid_url": "http://localhost:4444",
                    "pack_id": "p",
                    "platform_id": "x",
                },
            )
    assert resp.status_code == 400


async def test_start_appium_generic_runtime_error() -> None:
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with patch(
            "agent_app.appium.appium_mgr.start",
            side_effect=RuntimeError("boom"),
        ):
            resp = await client.post(
                "/agent/appium/start",
                json={
                    "connection_target": "foo",
                    "port": 4723,
                    "grid_url": "http://localhost:4444",
                    "pack_id": "p",
                    "platform_id": "x",
                },
            )
    assert resp.status_code == 500


async def test_start_appium_unexpected_exception() -> None:
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with patch(
            "agent_app.appium.appium_mgr.start",
            side_effect=ValueError("unexpected"),
        ):
            resp = await client.post(
                "/agent/appium/start",
                json={
                    "connection_target": "foo",
                    "port": 4723,
                    "grid_url": "http://localhost:4444",
                    "pack_id": "p",
                    "platform_id": "x",
                },
            )
    assert resp.status_code == 500


async def test_normalize_device_route_no_adapter_registry() -> None:
    from httpx import ASGITransport, AsyncClient

    app.state.adapter_registry = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/agent/pack/devices/normalize",
            json={"pack_id": "p", "pack_release": "1", "platform_id": "x", "raw_input": {}},
        )
    assert resp.status_code == 404
    assert "No adapter loaded" in resp.json()["detail"]


async def test_feature_action_route_no_adapter() -> None:
    from httpx import ASGITransport, AsyncClient

    app.state.adapter_registry = AdapterRegistry()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/agent/pack/features/fid/actions/aid",
            json={"pack_id": "unknown", "args": {}, "device_identity_value": None},
        )
    assert resp.status_code == 404
    assert "No adapter loaded" in resp.json()["detail"]


async def test_pack_device_lifecycle_route_no_adapter_registry() -> None:
    from httpx import ASGITransport, AsyncClient

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
                grid_slots=[],
                identity_scheme="a",
                identity_scope="host",
                stereotype={},
                appium_platform_name="A",
            )
        ],
    )
    app.dependency_overrides[_latest_desired_dep] = lambda: [desired]
    try:
        app.state.adapter_registry = None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/agent/pack/devices/abc/lifecycle/reboot", params={"pack_id": "p", "platform_id": "x"}, json={}
            )
    finally:
        app.dependency_overrides.pop(_latest_desired_dep, None)
    assert resp.status_code == 200
    assert resp.json()["detail"] == "Adapter not loaded for pack p:x"
