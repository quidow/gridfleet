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
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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

    health = AsyncMock()
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
            "app.devices.services.connectivity._fetch_lifecycle_state",
            new_callable=AsyncMock,
        ),
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
            health=health,
        ).check_connectivity(db_session)

    # The first fence is the post-probe fence (before the device loop). The
    # lifecycle fetch now runs inside the concurrent probe phase ahead of that
    # fence (DEBT-4); the fence still aborts the apply loop, so no per-device
    # state mutation occurs. Pins that fence: removing it would surface here.
    health.update_device_checks.assert_not_called()
    health.update_emulator_state.assert_not_called()


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
            side_effect=[None, None, LeadershipLost("site stop")],
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
    # Fence sequence to this branch (per-device hot-path fence removed): post-probe
    # (1), after-enumeration (2), then the disconnected-branch fence (3) raises
    # before _stop_disconnected_node. Pins the abort to that fence.
    assert mock_leader.await_count == 3


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
            "app.devices.services.connectivity._fetch_lifecycle_state",
            new_callable=AsyncMock,
        ),
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
    # Aborted at the post-probe fence, before the apply loop: device state is
    # untouched (the lifecycle fetch now runs in the probe phase ahead of the fence).
    assert device.operational_state == initial_state


@pytest.mark.db
@pytest.mark.asyncio
async def test_check_connectivity_commits_per_device(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A mid-cycle abort must not roll back earlier devices' committed writes.

    The cycle previously ran as ONE transaction (single commit at the end), so
    the first device's row lock was held across every later device's agent HTTP
    call — measured holds up to 2.3s blocked the allocator. Pins the
    commit-per-device boundary: device 1's state-store write survives an abort
    during device 2.
    """
    from app.core.leader import state_store as control_plane_state_store
    from app.devices.services.connectivity import PROBE_UNANSWERED_NAMESPACE

    host = Host(
        id=uuid.uuid4(),
        hostname="conn-pc-h",
        ip="10.0.0.46",
        agent_port=5100,
        status=HostStatus.online,
        os_type=OSType.linux,
    )
    db_session.add(host)
    await db_session.flush()
    identity_values = ["conn-pc-001", "conn-pc-002"]
    with state_write_guard.bypass():
        for identity_value in identity_values:
            db_session.add(
                Device(
                    pack_id="appium-uiautomator2",
                    platform_id="android_mobile",
                    identity_scheme="android_serial",
                    identity_scope="host",
                    identity_value=identity_value,
                    connection_target=identity_value,
                    name=f"Conn PC {identity_value}",
                    os_version="14",
                    host_id=host.id,
                    operational_state=DeviceOperationalState.available,
                    device_type=DeviceType.real_device,
                    connection_type=ConnectionType.usb,
                )
            )
    await db_session.commit()

    with (
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value=set(),  # enumeration reachable but reports no devices -> disconnected branch
        ),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new=AsyncMock(return_value=None),  # probe unanswered for both devices
        ),
        patch(
            "app.devices.services.connectivity._fetch_lifecycle_state",
            new=AsyncMock(return_value=None),
        ),
        # No node is observed_running, so _stop_disconnected_node is reached.
        patch(
            "app.devices.services.connectivity._stop_disconnected_node",
            new=AsyncMock(return_value=None),
        ),
        # Fence sequence with the per-device hot-path fence removed and a high
        # unanswered threshold (no early escalation): post-probe (1); device-1
        # after-enumeration (2); device-1 pre-stop (3); device-2 commits device-1's
        # disconnect write at the top of its iteration, then device-2 pre-stop (4)
        # raises. (Enumeration is cached per host, so it is not re-fenced for device 2.)
        patch(
            "app.devices.services.connectivity.assert_current_leader",
            side_effect=[None, None, None, LeadershipLost("mid-cycle")],
        ),
        pytest.raises(LeadershipLost),
    ):
        await ConnectivityService(
            publisher=event_bus,
            settings=FakeSettingsReader({"device_checks.probe_unanswered.consecutive_fail_threshold": 5}),
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=AsyncMock(),
        ).check_connectivity(db_session)

    # Verify from a FRESH session: device-1's probe_unanswered counter (written
    # before its disconnect handling and committed at the top of device-2's
    # iteration) survived the abort during device-2. Exactly one device committed
    # (loop order between the two devices is not guaranteed, so count both).
    async with db_session_maker() as verify:
        values = [
            await control_plane_state_store.get_value(verify, PROBE_UNANSWERED_NAMESPACE, iv) for iv in identity_values
        ]
    committed = [v for v in values if v is not None]
    assert len(committed) == 1, f"expected exactly one committed probe_unanswered counter, got {values!r}"
