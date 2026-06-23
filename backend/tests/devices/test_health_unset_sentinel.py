"""apply_node_state_transition UNSET semantics.

Omitted health kwargs must NOT write the columns (regression: observed-start
writes were wiping health_state='error', flapping the public summary during
failed recovery retries). Explicit None keeps its 'clear' meaning.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services import state_write_guard
from app.devices.services.health import DeviceHealthService
from app.devices.services.health_view import build_public_summary
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


async def _seed_device_with_error_node(db_session: AsyncSession, db_host: Host, identity: str) -> Device:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value=identity,
            connection_target=identity,
            name=f"Sentinel Phone {identity}",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.offline,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4760,
            desired_state=AppiumDesiredState.running,
            desired_port=4760,
            pid=1,
            active_connection_target="target",
            health_running=False,
            health_state="error",
            consecutive_health_failures=1,
        )
    db_session.add(node)
    await db_session.commit()
    return device


async def test_omitted_health_kwargs_do_not_clear_error(db_session: AsyncSession, db_host: Host) -> None:
    device = await _seed_device_with_error_node(db_session, db_host, "sentinel-1")

    await DeviceHealthService(publisher=event_bus).apply_node_state_transition(
        db_session,
        device,
        mark_offline=False,
    )
    await db_session.commit()

    await db_session.refresh(device, attribute_names=["appium_node"])
    assert device.appium_node is not None
    assert device.appium_node.health_state == "error"
    assert device.appium_node.health_running is False
    assert build_public_summary(device)["node"]["status"] == "failed"


async def test_explicit_none_still_clears(db_session: AsyncSession, db_host: Host) -> None:
    device = await _seed_device_with_error_node(db_session, db_host, "sentinel-2")

    await DeviceHealthService(publisher=event_bus).apply_node_state_transition(
        db_session,
        device,
        health_running=None,
        health_state=None,
        mark_offline=False,
    )
    await db_session.commit()

    await db_session.refresh(device, attribute_names=["appium_node"])
    assert device.appium_node is not None
    assert device.appium_node.health_state is None
    assert device.appium_node.health_running is None


async def test_omitted_health_kwargs_do_not_bump_last_checked(db_session: AsyncSession, db_host: Host) -> None:
    device = await _seed_device_with_error_node(db_session, db_host, "sentinel-3")
    await db_session.refresh(device, attribute_names=["appium_node"])
    assert device.appium_node is not None
    before = device.appium_node.last_health_checked_at

    await DeviceHealthService(publisher=event_bus).apply_node_state_transition(
        db_session,
        device,
        mark_offline=False,
    )
    await db_session.commit()

    await db_session.refresh(device, attribute_names=["appium_node"])
    assert device.appium_node is not None
    assert device.appium_node.last_health_checked_at == before
