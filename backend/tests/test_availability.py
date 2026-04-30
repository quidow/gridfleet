from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import ConnectionType, Device, DeviceAvailabilityStatus, DeviceType
from app.models.host import Host
from app.services import device_health_summary

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_availability_enough_devices(client: AsyncClient, db_session: AsyncSession, db_host: Host) -> None:
    for i in range(3):
        db_session.add(
            Device(
                pack_id="appium-uiautomator2",
                platform_id="android_mobile",
                identity_scheme="android_serial",
                identity_scope="host",
                identity_value=f"avail-{i}",
                connection_target=f"avail-{i}",
                name=f"Phone {i}",
                os_version="14",
                host_id=db_host.id,
                availability_status=DeviceAvailabilityStatus.available,
                verified_at=datetime.now(UTC),
                device_type=DeviceType.real_device,
                connection_type=ConnectionType.usb,
            )
        )
    await db_session.commit()

    resp = await client.get("/api/availability", params={"platform_id": "android_mobile", "count": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert data["matched"] == 3
    assert data["requested"] == 2
    assert data["platform_id"] == "android_mobile"


async def test_availability_not_enough_devices(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    db_session.add(
        Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="avail-solo",
            connection_target="avail-solo",
            name="Solo Phone",
            os_version="14",
            host_id=db_host.id,
            availability_status=DeviceAvailabilityStatus.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    )
    db_session.add(
        Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="avail-busy",
            connection_target="avail-busy",
            name="Busy Phone",
            os_version="14",
            host_id=db_host.id,
            availability_status=DeviceAvailabilityStatus.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    )
    await db_session.commit()

    resp = await client.get("/api/availability", params={"platform_id": "android_mobile", "count": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is False
    assert data["matched"] == 1
    assert data["requested"] == 2


async def test_availability_excludes_unhealthy_devices(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    unhealthy = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="avail-unhealthy",
        connection_target="avail-unhealthy",
        name="Unhealthy Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(unhealthy)
    await db_session.commit()
    await db_session.refresh(unhealthy)

    await device_health_summary.update_device_checks(
        db_session,
        unhealthy,
        healthy=False,
        summary="ADB not responsive",
    )
    await db_session.commit()

    resp = await client.get("/api/availability", params={"platform_id": "android_mobile", "count": 1})
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is False
    assert data["matched"] == 0

    device_resp = await client.get(f"/api/devices/{unhealthy.id}")
    assert device_resp.status_code == 200
    device_data = device_resp.json()
    assert device_data["availability_status"] == DeviceAvailabilityStatus.offline.value
    assert device_data["health_summary"]["healthy"] is False


async def test_availability_restores_when_unhealthy_offline_device_recovers(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="avail-recovered",
        connection_target="avail-recovered",
        name="Recovered Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            state=NodeState.running,
        )
    )
    await db_session.commit()
    await db_session.refresh(device, ["appium_node"])

    await device_health_summary.update_node_state(db_session, device, running=False, state="error")
    await db_session.commit()
    assert device.availability_status == DeviceAvailabilityStatus.offline

    await device_health_summary.update_device_checks(db_session, device, healthy=True, summary="Healthy")
    await device_health_summary.update_session_viability(db_session, device, status="passed", error=None)
    await device_health_summary.update_node_state(db_session, device, running=True, state="running")
    await db_session.commit()

    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.available

    resp = await client.get("/api/availability", params={"platform_id": "android_mobile", "count": 1})
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert data["matched"] == 1


async def test_availability_wrong_platform(client: AsyncClient, db_session: AsyncSession, db_host: Host) -> None:
    db_session.add(
        Device(
            pack_id="appium-xcuitest",
            platform_id="ios",
            identity_scheme="apple_udid",
            identity_scope="global",
            identity_value="avail-ios",
            connection_target="avail-ios",
            name="iPhone",
            os_version="17",
            host_id=db_host.id,
            availability_status=DeviceAvailabilityStatus.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    )
    await db_session.commit()

    resp = await client.get("/api/availability", params={"platform_id": "android_mobile", "count": 1})
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is False
    assert data["matched"] == 0


async def test_availability_requires_platform(client: AsyncClient) -> None:
    resp = await client.get("/api/availability", params={"count": 1})
    assert resp.status_code == 422


async def test_device_logs_no_node(client: AsyncClient, db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="logs-001",
        connection_target="logs-001",
        name="Log Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.offline,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    resp = await client.get(f"/api/devices/{device.id}/logs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["lines"] == []
    assert data["count"] == 0


async def test_availability_excludes_unverified_devices(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    db_session.add(
        Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="avail-verified",
            connection_target="avail-verified",
            name="Verified Phone",
            os_version="14",
            host_id=db_host.id,
            availability_status=DeviceAvailabilityStatus.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    )
    db_session.add(
        Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="avail-unverified",
            connection_target="avail-unverified",
            name="Unverified Phone",
            os_version="14",
            host_id=db_host.id,
            availability_status=DeviceAvailabilityStatus.available,
            verified_at=None,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    )
    await db_session.commit()

    resp = await client.get("/api/availability", params={"platform_id": "android_mobile", "count": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is False
    assert data["matched"] == 1
