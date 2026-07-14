from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest_asyncio

from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services.connectivity import ConnectivityService
from app.devices.services.health import DeviceHealthService
from app.hosts.models import Host, HostStatus
from tests.fakes import FakeSettingsReader

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest_asyncio.fixture
async def connectivity_service() -> ConnectivityService:
    return ConnectivityService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        lifecycle_policy=AsyncMock(),
        health=DeviceHealthService(publisher=Mock()),
    )


@pytest_asyncio.fixture
async def host_with_two_devices(db_session: AsyncSession) -> SimpleNamespace:
    host = Host(hostname="v7-fold-host", ip="10.0.0.30", os_type="linux", agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()
    devices: list[Device] = []
    for index in range(2):
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value=f"v7-fold-dev-{index}",
            connection_target=f"v7-fold-dev-{index}",
            name=f"V7 Fold Device {index}",
            os_version="14",
            host_id=host.id,
            operational_state=DeviceOperationalState.available,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            # Verified + healthy baseline so the device derives ``available`` (not
            # ``offline``); an absent verdict then produces a real disconnect edge.
            verified_at=datetime.now(UTC),
            device_checks_healthy=True,
        )
        db_session.add(device)
        devices.append(device)
    await db_session.commit()
    for device in devices:
        await db_session.refresh(device, attribute_names=["appium_node", "host"])
    return SimpleNamespace(id=host.id, devices=devices)
