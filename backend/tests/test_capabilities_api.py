from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.models.appium_node import AppiumNode, NodeState
from app.services import appium_node_resource_service
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
    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", state=NodeState.running)
    db_session.add(node)
    await db_session.flush()
    expected_caps = {
        "appium:systemPort": await appium_node_resource_service.reserve(
            db_session,
            host_id=device.host_id,
            capability_key="appium:systemPort",
            start_port=8200,
            node_id=node.id,
        ),
        "appium:chromedriverPort": await appium_node_resource_service.reserve(
            db_session,
            host_id=device.host_id,
            capability_key="appium:chromedriverPort",
            start_port=9515,
            node_id=node.id,
        ),
        "appium:mjpegServerPort": await appium_node_resource_service.reserve(
            db_session,
            host_id=device.host_id,
            capability_key="appium:mjpegServerPort",
            start_port=9200,
            node_id=node.id,
        ),
    }
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
    node = AppiumNode(device_id=device.id, port=4725, grid_url="http://hub:4444", state=NodeState.running)
    db_session.add(node)
    await db_session.flush()
    expected_caps = {
        "appium:wdaLocalPort": await appium_node_resource_service.reserve(
            db_session,
            host_id=device.host_id,
            capability_key="appium:wdaLocalPort",
            start_port=8100,
            node_id=node.id,
        ),
        "appium:mjpegServerPort": await appium_node_resource_service.reserve(
            db_session,
            host_id=device.host_id,
            capability_key="appium:mjpegServerPort",
            start_port=9100,
            node_id=node.id,
        ),
        "appium:derivedDataPath": "/tmp/gridfleet/derived-data/test",
    }
    await appium_node_resource_service.set_node_extra_capability(
        db_session,
        node_id=node.id,
        capability_key="appium:derivedDataPath",
        value=expected_caps["appium:derivedDataPath"],
    )
    await db_session.commit()

    resp = await client.get(f"/api/devices/{device.id}/capabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["appium:updatedWDABundleId"] == "com.example.test"
    assert data["appium:wdaLocalPort"] == expected_caps["appium:wdaLocalPort"]
    assert data["appium:mjpegServerPort"] == expected_caps["appium:mjpegServerPort"]
    assert data["appium:derivedDataPath"] == expected_caps["appium:derivedDataPath"]
