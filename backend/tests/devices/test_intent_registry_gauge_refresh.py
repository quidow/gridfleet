"""INTENT_REGISTRY_INTENTS is published once per /metrics scrape by the devices
gauge refresher, not on every reconcile_device call (which ran a full-table
COUNT(*) on the request hot path — see AUDIT.md §5.1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from prometheus_client import REGISTRY

from app.devices import _refresh_devices_gauges
from app.devices.models import (
    ConnectionType,
    Device,
    DeviceIntent,
    DeviceOperationalState,
    DeviceType,
)
from app.devices.services.intent_types import NODE_PROCESS
from app.hosts.models import Host, HostStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.db, pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


def _gauge() -> float:
    return REGISTRY.get_sample_value("intent_registry_intents_total") or 0.0


async def test_refresher_publishes_device_intent_count(db_session: AsyncSession) -> None:
    # Baseline from whatever rows already exist in this test transaction.
    await _refresh_devices_gauges(db_session)
    before = _gauge()

    host = Host(
        id=uuid.uuid4(),
        hostname="intent-gauge-h",
        ip="10.0.2.1",
        agent_port=5100,
        status=HostStatus.online,
        os_type="linux",
    )
    db_session.add(host)
    await db_session.flush()
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="intent-gauge-001",
        connection_target="intent-gauge-001",
        name="Intent Gauge Device",
        os_version="14",
        host_id=host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    for i in range(3):
        db_session.add(
            DeviceIntent(
                device_id=device.id,
                source=f"test:intent:{i}",
                axis=NODE_PROCESS,
                payload={"action": "start"},
            )
        )
    await db_session.commit()

    await _refresh_devices_gauges(db_session)
    assert _gauge() == before + 3
