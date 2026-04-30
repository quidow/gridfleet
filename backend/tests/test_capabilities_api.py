from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.models.appium_node import AppiumNode, NodeState
from app.services import appium_resource_allocator
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_capabilities_endpoint_hides_runtime_allocations_for_idle_device(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="caps-idle-001",
        connection_target="caps-idle-001",
        name="Idle Caps Device",
        device_config={
            "appium_caps": {
                "appium:noReset": True,
                "appium:systemPort": 9999,
            }
        },
    )

    resp = await client.get(f"/api/devices/{device.id}/capabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["appium:noReset"] is True
    assert "appium:systemPort" not in data
    assert "appium:chromedriverPort" not in data
    assert "appium:mjpegServerPort" not in data


async def test_capabilities_endpoint_returns_live_android_allocations_for_running_node(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="caps-android-001",
        connection_target="caps-android-001",
        name="Running Android Caps",
        device_config={
            "appium_caps": {
                "appium:noReset": True,
                "appium:systemPort": 9999,
            }
        },
        availability_status="available",
    )
    owner_key = appium_resource_allocator.managed_owner_key(device.id)
    expected_caps = await appium_resource_allocator.get_or_create_owner_capabilities(
        db_session,
        owner_key=owner_key,
        host_id=device.host_id,
        resource_ports={
            "appium:systemPort": 8200,
            "appium:chromedriverPort": 9515,
            "appium:mjpegServerPort": 9200,
        },
    )
    db_session.add(AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", state=NodeState.running))
    await db_session.commit()

    resp = await client.get(f"/api/devices/{device.id}/capabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["appium:noReset"] is True
    assert data["appium:systemPort"] == expected_caps["appium:systemPort"]
    assert data["appium:chromedriverPort"] == expected_caps["appium:chromedriverPort"]
    assert data["appium:mjpegServerPort"] == expected_caps["appium:mjpegServerPort"]


async def test_capabilities_endpoint_uses_active_target_for_running_avd(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="avd:Pixel_6_API_35",
        connection_target="Pixel_6_API_35",
        name="Pixel 6 AVD",
        platform_id="android_mobile",
        device_type="emulator",
        availability_status="available",
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            active_connection_target="emulator-5554",
            state=NodeState.running,
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/devices/{device.id}/capabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["appium:udid"] == "emulator-5554"
    assert data["appium:gridfleet:deviceId"] == str(device.id)


async def test_capabilities_endpoint_returns_live_xcuitest_allocations_for_running_node(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="caps-ios-001",
        connection_target="caps-ios-001",
        name="Running iOS Caps",
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="apple_udid",
        identity_scope="global",
        device_type="real_device",
        availability_status="available",
        device_config={
            "appium_caps": {
                "appium:updatedWDABundleId": "com.example.test",
                "appium:wdaLocalPort": 9999,
            }
        },
    )
    owner_key = appium_resource_allocator.managed_owner_key(device.id)
    expected_caps = await appium_resource_allocator.get_or_create_owner_capabilities(
        db_session,
        owner_key=owner_key,
        host_id=device.host_id,
        resource_ports={
            "appium:wdaLocalPort": 8100,
            "appium:mjpegServerPort": 9100,
        },
        needs_derived_data_path=True,
    )
    db_session.add(AppiumNode(device_id=device.id, port=4725, grid_url="http://hub:4444", state=NodeState.running))
    await db_session.commit()

    resp = await client.get(f"/api/devices/{device.id}/capabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["appium:updatedWDABundleId"] == "com.example.test"
    assert data["appium:wdaLocalPort"] == expected_caps["appium:wdaLocalPort"]
    assert data["appium:mjpegServerPort"] == expected_caps["appium:mjpegServerPort"]
    assert data["appium:derivedDataPath"] == expected_caps["appium:derivedDataPath"]
