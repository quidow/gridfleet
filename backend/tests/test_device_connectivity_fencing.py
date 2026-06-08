"""device_connectivity_loop must not mutate device state after losing leadership."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.core.leader.advisory import LeadershipLost
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services import state_write_guard
from app.devices.services.connectivity import ConnectivityService
from app.hosts.models import Host, HostStatus, OSType
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.db
@pytest.mark.asyncio
async def test_check_connectivity_aborts_after_agent_call_when_leadership_lost(
    db_session: AsyncSession,
) -> None:
    host = Host(
        id=uuid.uuid4(),
        hostname="conn-h1",
        ip="10.0.0.42",
        agent_port=5100,
        status=HostStatus.online,
        os_type=OSType.linux,
    )
    db_session.add(host)
    await db_session.flush()
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="conn-h1-001",
            connection_target="conn-h1-001",
            name="Conn H1 Device",
            os_version="14",
            host_id=host.id,
            operational_state=DeviceOperationalState.available,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()

    with (
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.devices.services.connectivity._get_lifecycle_state",
            new_callable=AsyncMock,
        ) as mock_lifecycle,
        patch(
            "app.devices.services.connectivity.assert_current_leader",
            side_effect=LeadershipLost("test"),
        ),
        pytest.raises(LeadershipLost),
    ):
        await ConnectivityService(
            publisher=event_bus,
            settings=FakeSettingsReader({}),
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=AsyncMock(),
        ).check_connectivity(db_session)

    # The first fence is the post-probe fence (before the device loop): aborting
    # there means the per-device lifecycle probe is never reached. Pins that fence
    # so its removal would surface here instead of silently passing.
    mock_lifecycle.assert_not_called()


@pytest.mark.db
@pytest.mark.asyncio
async def test_check_connectivity_aborts_in_connected_branch_when_leadership_lost(
    db_session: AsyncSession,
) -> None:
    host = Host(
        id=uuid.uuid4(),
        hostname="conn-b-h",
        ip="10.0.0.43",
        agent_port=5100,
        status=HostStatus.online,
        os_type=OSType.linux,
    )
    db_session.add(host)
    await db_session.flush()
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="conn-b-001",
            connection_target="conn-b-001",
            name="Conn B Device",
            os_version="14",
            host_id=host.id,
            operational_state=DeviceOperationalState.available,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()
    initial_state = device.operational_state

    with (
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value={"conn-b-001"},
        ),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
        patch(
            "app.devices.services.connectivity.assert_current_leader",
            side_effect=[None, LeadershipLost("site b")],
        ) as mock_leader,
        pytest.raises(LeadershipLost),
    ):
        await ConnectivityService(
            publisher=event_bus,
            settings=FakeSettingsReader({}),
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=AsyncMock(),
        ).check_connectivity(db_session)

    await db_session.refresh(device, attribute_names=["operational_state"])
    assert device.operational_state == initial_state
    # Post-probe fence passed (call 1), after-lifecycle fence raised (call 2),
    # before the connected-device write path. Pins the abort to that fence.
    assert mock_leader.await_count == 2


@pytest.mark.db
@pytest.mark.asyncio
async def test_check_connectivity_aborts_before_stop_disconnected_node_when_leadership_lost(
    db_session: AsyncSession,
) -> None:
    """Cover the fence guarding _stop_disconnected_node in the disconnected branch."""
    host = Host(
        id=uuid.uuid4(),
        hostname="conn-stop-h",
        ip="10.0.0.45",
        agent_port=5100,
        status=HostStatus.online,
        os_type=OSType.linux,
    )
    db_session.add(host)
    await db_session.flush()
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="conn-stop-001",
            connection_target="conn-stop-001",
            name="Conn Stop Device",
            os_version="14",
            host_id=host.id,
            operational_state=DeviceOperationalState.available,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()
    initial_state = device.operational_state

    stop_called = AsyncMock()

    with (
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value=set(),
        ),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.devices.services.connectivity._stop_disconnected_node",
            new=stop_called,
        ),
        patch(
            "app.devices.services.connectivity.assert_current_leader",
            side_effect=[None, None, None, LeadershipLost("site stop")],
        ) as mock_leader,
        pytest.raises(LeadershipLost),
    ):
        await ConnectivityService(
            publisher=event_bus,
            settings=FakeSettingsReader({}),
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=AsyncMock(),
        ).check_connectivity(db_session)

    stop_called.assert_not_called()
    await db_session.refresh(device, attribute_names=["operational_state"])
    assert device.operational_state == initial_state
    # Fence sequence to this branch: post-probe (1), after-lifecycle (2),
    # after-enumeration (3), then the disconnected-branch fence (4) raises before
    # _stop_disconnected_node. Pins the abort to that fence.
    assert mock_leader.await_count == 4


@pytest.mark.db
@pytest.mark.asyncio
async def test_check_connectivity_post_probe_fence_aborts_before_connected_write(
    db_session: AsyncSession,
) -> None:
    """The health probe now runs in the concurrent phase, re-fenced once afterward.

    A device that would otherwise be written (healthy) must not be touched when
    leadership is lost across that probe phase.
    """
    host = Host(
        id=uuid.uuid4(),
        hostname="conn-c-h",
        ip="10.0.0.44",
        agent_port=5100,
        status=HostStatus.online,
        os_type=OSType.linux,
    )
    db_session.add(host)
    await db_session.flush()
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="conn-c-001",
            connection_target="conn-c-001",
            name="Conn C Device",
            os_version="14",
            host_id=host.id,
            operational_state=DeviceOperationalState.available,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()
    initial_state = device.operational_state

    with (
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value=set(),
        ),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
        patch(
            "app.devices.services.connectivity._get_lifecycle_state",
            new_callable=AsyncMock,
        ) as mock_lifecycle,
        patch(
            "app.devices.services.connectivity.assert_current_leader",
            side_effect=LeadershipLost("site c"),
        ),
        pytest.raises(LeadershipLost),
    ):
        await ConnectivityService(
            publisher=event_bus,
            settings=FakeSettingsReader({}),
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=AsyncMock(),
        ).check_connectivity(db_session)

    await db_session.refresh(device, attribute_names=["operational_state"])
    assert device.operational_state == initial_state
    # Aborted at the post-probe fence, before the device loop: lifecycle never ran.
    mock_lifecycle.assert_not_called()
