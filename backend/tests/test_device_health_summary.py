from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from app.models.device import ConnectionType, Device, DeviceAvailabilityStatus, DeviceType
from app.services import device_health_summary

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _make_device(db_host: Host, identity: str) -> Device:
    return Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=identity,
        connection_target=identity,
        name=f"Device {identity}",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )


async def test_publishes_event_on_healthy_to_unhealthy_transition(db_session: AsyncSession, db_host: Host) -> None:
    device = _make_device(db_host, "dhs-1")
    db_session.add(device)
    await db_session.commit()

    # Seed snapshot as healthy
    await device_health_summary.update_node_state(db_session, device, running=True, state="running")
    await device_health_summary.update_device_checks(db_session, device, healthy=True, summary="Healthy")
    await db_session.commit()

    publish = AsyncMock()
    with patch("app.services.device_health_summary.event_bus.publish", publish):
        await device_health_summary.update_node_state(
            db_session, device, running=False, state="error", mark_offline_on_failure=False
        )

    health_calls = [c for c in publish.await_args_list if c.args and c.args[0] == "device.health_changed"]
    assert len(health_calls) == 1
    payload: dict[str, Any] = health_calls[0].args[1]
    assert payload["device_id"] == str(device.id)
    assert payload["healthy"] is False
    assert isinstance(payload["summary"], str)


async def test_no_event_when_healthy_unchanged(db_session: AsyncSession, db_host: Host) -> None:
    device = _make_device(db_host, "dhs-2")
    db_session.add(device)
    await db_session.commit()

    await device_health_summary.update_node_state(db_session, device, running=True, state="running")
    await device_health_summary.update_device_checks(db_session, device, healthy=True, summary="Healthy")
    await db_session.commit()

    publish = AsyncMock()
    with patch("app.services.device_health_summary.event_bus.publish", publish):
        # Same value again — only timestamp changes, healthy stays True
        await device_health_summary.update_device_checks(db_session, device, healthy=True, summary="Healthy")

    health_calls = [c for c in publish.await_args_list if c.args and c.args[0] == "device.health_changed"]
    assert health_calls == []


async def test_publishes_event_on_unhealthy_to_healthy_transition(db_session: AsyncSession, db_host: Host) -> None:
    device = _make_device(db_host, "dhs-3")
    db_session.add(device)
    await db_session.commit()

    await device_health_summary.update_device_checks(db_session, device, healthy=False, summary="Disconnected")
    await db_session.commit()

    publish = AsyncMock()
    with patch("app.services.device_health_summary.event_bus.publish", publish):
        await device_health_summary.update_device_checks(db_session, device, healthy=True, summary="Healthy")

    health_calls = [c for c in publish.await_args_list if c.args and c.args[0] == "device.health_changed"]
    assert len(health_calls) == 1
    assert health_calls[0].args[1]["healthy"] is True
