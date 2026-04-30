from typing import ClassVar

import pytest
from httpx import ASGITransport, AsyncClient

from agent_app.main import app


@pytest.mark.asyncio
async def test_pack_device_properties_endpoint_uses_latest_desired_packs(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_app.pack.manifest import AppiumInstallable, DesiredPack, DesiredPlatform

    desired = DesiredPack(
        id="appium-uiautomator2",
        release="1.0",
        appium_server=AppiumInstallable("npm", "appium", "2.11.5", None, []),
        appium_driver=AppiumInstallable("npm", "appium-uiautomator2-driver", "3.6.0", None, []),
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

    class Loop:
        latest_desired_packs: ClassVar[list[DesiredPack]] = [desired]

    async def fake_enumerate(*args: object, **kwargs: object) -> dict[str, object]:
        assert args[0] == [desired]
        return {
            "candidates": [
                {
                    "pack_id": "appium-uiautomator2",
                    "platform_id": "android_mobile",
                    "identity_scheme": "android_serial",
                    "identity_scope": "host",
                    "identity_value": "stable-serial",
                    "suggested_name": "Pixel",
                    "detected_properties": {
                        "connection_target": "adb-transport",
                        "os_version": "15",
                        "ip_address": "10.0.0.25",
                    },
                    "runnable": True,
                    "missing_requirements": [],
                }
            ],
        }

    monkeypatch.setattr(app.state, "pack_state_loop", Loop(), raising=False)
    monkeypatch.setattr("agent_app.pack.discovery.enumerate_pack_candidates", fake_enumerate)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/agent/pack/devices/stable-serial/properties",
            params={"pack_id": "appium-uiautomator2"},
        )

    assert resp.status_code == 200
    assert resp.json()["detected_properties"]["os_version"] == "15"


@pytest.mark.asyncio
async def test_pack_device_properties_not_found() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/agent/pack/devices/NONEXISTENT/properties",
            params={"pack_id": "appium-uiautomator2"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pack_devices_endpoint_returns_empty_without_desired_packs() -> None:
    """Without desired packs (no pack state loop), the endpoint returns empty candidates."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/agent/pack/devices")
    assert resp.status_code == 200
    body = resp.json()
    assert body["candidates"] == []
