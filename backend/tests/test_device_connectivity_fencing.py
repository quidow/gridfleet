"""device_connectivity_loop must not mutate device state after losing leadership."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from app.models.device import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.models.host import Host, HostStatus, OSType
from app.services.control_plane_leader import LeadershipLost
from app.services.device_connectivity import _check_connectivity

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
    await db_session.commit()

    with (
        patch(
            "app.services.device_connectivity._get_agent_devices",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "app.services.device_connectivity.assert_current_leader",
            side_effect=LeadershipLost("test"),
        ),
        pytest.raises(LeadershipLost),
    ):
        await _check_connectivity(db_session)


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
            "app.services.device_connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value={"conn-b-001"},
        ),
        patch(
            "app.services.device_connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
        patch(
            "app.services.device_connectivity.assert_current_leader",
            side_effect=[None, None, LeadershipLost("site b")],
        ),
        pytest.raises(LeadershipLost),
    ):
        await _check_connectivity(db_session)

    await db_session.refresh(device, attribute_names=["operational_state"])
    assert device.operational_state == initial_state


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
        auto_manage=True,
    )
    db_session.add(device)
    await db_session.commit()
    initial_state = device.operational_state

    stop_called = AsyncMock()

    with (
        patch(
            "app.services.device_connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value=set(),
        ),
        patch(
            "app.services.device_connectivity._uses_endpoint_health",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "app.services.device_connectivity._stop_disconnected_node",
            new=stop_called,
        ),
        patch(
            "app.services.device_connectivity.assert_current_leader",
            side_effect=[None, None, LeadershipLost("site stop")],
        ),
        pytest.raises(LeadershipLost),
    ):
        await _check_connectivity(db_session)

    stop_called.assert_not_called()
    await db_session.refresh(device, attribute_names=["operational_state"])
    assert device.operational_state == initial_state


@pytest.mark.db
@pytest.mark.asyncio
async def test_check_connectivity_aborts_in_endpoint_health_branch_when_leadership_lost(
    db_session: AsyncSession,
) -> None:
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
            "app.services.device_connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value=set(),
        ),
        patch(
            "app.services.device_connectivity._uses_endpoint_health",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.services.device_connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
        patch(
            "app.services.device_connectivity.assert_current_leader",
            side_effect=[None, None, LeadershipLost("site c")],
        ),
        pytest.raises(LeadershipLost),
    ):
        await _check_connectivity(db_session)

    await db_session.refresh(device, attribute_names=["operational_state"])
    assert device.operational_state == initial_state
