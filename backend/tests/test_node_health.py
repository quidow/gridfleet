import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AgentResponseError, AgentUnreachableError, CircuitOpenError
from app.models.appium_node import AppiumNode, NodeState
from app.models.device import ConnectionType, Device, DeviceAvailabilityStatus, DeviceType
from app.models.device_event import DeviceEvent, DeviceEventType
from app.models.host import Host, HostStatus
from app.services import device_health
from app.services.agent_probe_result import ProbeResult
from app.services.node_health import (
    _check_node_health,
    _check_nodes,
    _should_probe_node_health,
)
from app.services.node_service_types import NodeManagerError

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def set_node_health_failure_count(db_session: AsyncSession, node_key: str, count: int) -> None:
    node = await db_session.get(AppiumNode, uuid.UUID(node_key))
    assert node is not None
    node.consecutive_health_failures = count
    await db_session.commit()


async def get_node_health_control_plane_state(db_session: AsyncSession) -> dict[str, int]:
    nodes = (await db_session.execute(select(AppiumNode))).scalars().all()
    return {str(node.id): node.consecutive_health_failures for node in nodes if node.consecutive_health_failures > 0}


async def test_healthy_node_clears_failure_count(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="nh-001",
        connection_target="nh-001",
        name="Healthy Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", state=NodeState.running)
    db_session.add(node)
    await db_session.commit()

    # Pre-set some failure counts
    await set_node_health_failure_count(db_session, str(node.id), 2)

    with patch("app.services.node_health._check_node_health", return_value=ProbeResult(status="ack")):
        await _check_nodes(db_session)

    assert str(node.id) not in await get_node_health_control_plane_state(db_session)
    await db_session.refresh(node)
    assert node.state == NodeState.running


async def test_unhealthy_node_increments_failure_count(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="nh-002",
        connection_target="nh-002",
        name="Failing Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(device_id=device.id, port=4724, grid_url="http://hub:4444", state=NodeState.running)
    db_session.add(node)
    await db_session.commit()

    with patch("app.services.node_health._check_node_health", return_value=ProbeResult(status="refused")):
        await _check_nodes(db_session)

    assert (await get_node_health_control_plane_state(db_session))[str(node.id)] == 1
    await db_session.refresh(node)
    assert node.state == NodeState.running  # Not yet at max
    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.available


async def test_node_missing_from_grid_increments_failure_count(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="nh-grid-missing",
        connection_target="nh-grid-missing",
        name="Missing Grid Relay Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4740,
        grid_url="http://hub:4444",
        state=NodeState.running,
        started_at=datetime.now(UTC) - timedelta(seconds=31),
    )
    db_session.add(node)
    await db_session.commit()

    with (
        patch("app.services.node_health._check_node_health", return_value=ProbeResult(status="ack")),
        patch(
            "app.services.node_health.grid_service.get_grid_status",
            new_callable=AsyncMock,
            return_value={"value": {"ready": False, "message": "Selenium Grid not ready.", "nodes": []}},
        ),
    ):
        await _check_nodes(db_session)

    assert (await get_node_health_control_plane_state(db_session))[str(node.id)] == 1
    await db_session.refresh(node)
    assert node.state == NodeState.running


async def test_fresh_node_missing_from_grid_waits_for_registration_grace(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="nh-grid-fresh",
        connection_target="nh-grid-fresh",
        name="Fresh Grid Relay Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4742,
        grid_url="http://hub:4444",
        state=NodeState.running,
        started_at=datetime.now(UTC),
    )
    db_session.add(node)
    await db_session.commit()

    with (
        patch("app.services.node_health._check_node_health", return_value=ProbeResult(status="ack")),
        patch(
            "app.services.node_health.grid_service.get_grid_status",
            new_callable=AsyncMock,
            return_value={"value": {"ready": False, "message": "Selenium Grid not ready.", "nodes": []}},
        ),
    ):
        await _check_nodes(db_session)

    assert str(node.id) not in await get_node_health_control_plane_state(db_session)
    await db_session.refresh(node)
    assert node.state == NodeState.running


async def test_node_registered_in_grid_clears_failure_count(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="nh-grid-present",
        connection_target="nh-grid-present",
        name="Registered Grid Relay Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(device_id=device.id, port=4741, grid_url="http://hub:4444", state=NodeState.running)
    db_session.add(node)
    await db_session.commit()
    await set_node_health_failure_count(db_session, str(node.id), 1)

    with (
        patch("app.services.node_health._check_node_health", return_value=ProbeResult(status="ack")),
        patch(
            "app.services.node_health.grid_service.get_grid_status",
            new_callable=AsyncMock,
            return_value={
                "value": {
                    "ready": True,
                    "nodes": [
                        {
                            "availability": "UP",
                            "slots": [
                                {
                                    "stereotype": {
                                        "appium:gridfleet:deviceId": str(device.id),
                                    }
                                }
                            ],
                        }
                    ],
                }
            },
        ),
    ):
        await _check_nodes(db_session)

    assert str(node.id) not in await get_node_health_control_plane_state(db_session)


async def test_node_restart_via_agent_on_max_failures(db_session: AsyncSession) -> None:
    """Node with a host triggers agent restart on max failures."""
    host = Host(hostname="test-host", ip="10.0.0.1", os_type="linux", agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="nh-004",
        connection_target="nh-004",
        name="Remote Phone",
        os_version="14",
        host_id=host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(device_id=device.id, port=4726, grid_url="http://hub:4444", state=NodeState.running)
    db_session.add(node)
    await db_session.commit()

    await set_node_health_failure_count(db_session, str(node.id), 2)

    with (
        patch("app.services.node_health._check_node_health", return_value=ProbeResult(status="refused")),
        patch(
            "app.services.node_health._restart_node_via_agent",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_restart,
    ):
        await _check_nodes(db_session)

    mock_restart.assert_called_once()
    await db_session.refresh(node)
    # Restart succeeded, so node stays running (agent mock sets it)
    assert str(node.id) not in await get_node_health_control_plane_state(db_session)


async def test_node_restart_failure_marks_offline(db_session: AsyncSession) -> None:
    """If agent restart fails, node goes to error and device goes offline."""
    host = Host(hostname="fail-host", ip="10.0.0.2", os_type="linux", agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="nh-005",
        connection_target="nh-005",
        name="Restart Fail Phone",
        os_version="14",
        host_id=host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(device_id=device.id, port=4727, grid_url="http://hub:4444", state=NodeState.running)
    db_session.add(node)
    await db_session.commit()

    await set_node_health_failure_count(db_session, str(node.id), 2)

    with (
        patch("app.services.node_health._check_node_health", return_value=ProbeResult(status="refused")),
        patch("app.services.node_health._restart_node_via_agent", new_callable=AsyncMock, return_value=False),
    ):
        await _check_nodes(db_session)

    await db_session.refresh(node)
    assert node.state == NodeState.error
    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.offline


async def test_missing_runtime_host_invariant_marks_node_offline(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="nh-006",
        connection_target="nh-006",
        name="Corrupted Runtime Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(device_id=device.id, port=4728, grid_url="http://hub:4444", state=NodeState.running)
    db_session.add(node)
    await db_session.commit()

    node_key = str(node.id)
    await set_node_health_failure_count(db_session, str(node.id), 2)

    with (
        patch(
            "app.services.node_health.require_management_host",
            side_effect=NodeManagerError("Device management host invariant is broken"),
        ),
        patch("app.services.node_health._restart_node_via_agent", new_callable=AsyncMock, return_value=False),
    ):
        await _check_nodes(db_session)

    await db_session.refresh(node)
    assert node.state == NodeState.error
    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.offline
    assert node_key not in await get_node_health_control_plane_state(db_session)


async def test_available_verified_node_uses_probe_session(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="nh-007",
        connection_target="nh-007",
        name="Probe-Safe Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        verified_at=datetime.now(UTC),
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(device_id=device.id, port=4729, grid_url="http://hub:4444", state=NodeState.running)
    db_session.add(node)
    await db_session.commit()

    with (
        patch(
            "app.services.node_health.capability_service.get_device_capabilities",
            new_callable=AsyncMock,
            return_value={"platformName": "Android"},
        ),
        patch(
            "app.services.node_health.fetch_appium_probe_session",
            new_callable=AsyncMock,
            return_value=(True, None),
        ) as probe_mock,
        patch("app.services.node_health.fetch_appium_status", new_callable=AsyncMock) as status_mock,
    ):
        await _check_nodes(db_session)

    probe_mock.assert_awaited_once()
    status_mock.assert_not_awaited()


async def test_real_ios_node_uses_status_fallback(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="apple_udid",
        identity_scope="global",
        identity_value="00008101-000A1234ABCD5678",
        connection_target="00008101-000A1234ABCD5678",
        name="Real iPhone",
        os_version="26.4.2",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        verified_at=datetime.now(UTC),
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(device_id=device.id, port=4734, grid_url="http://hub:4444", state=NodeState.running)
    db_session.add(node)
    await db_session.commit()

    with (
        patch(
            "app.services.node_health.capability_service.get_device_capabilities",
            new_callable=AsyncMock,
            return_value={"platformName": "iOS"},
        ) as capabilities_mock,
        patch("app.services.node_health.fetch_appium_probe_session", new_callable=AsyncMock) as probe_mock,
        patch(
            "app.services.node_health.fetch_appium_status",
            new_callable=AsyncMock,
            return_value={"running": True, "port": 4734},
        ) as status_mock,
    ):
        await _check_nodes(db_session)

    capabilities_mock.assert_not_awaited()
    probe_mock.assert_not_awaited()
    status_mock.assert_awaited_once()


@pytest.mark.parametrize("platform_id", ["ios", "tvos"])
async def test_real_apple_node_health_probe_gate_is_disabled(
    db_session: AsyncSession,
    db_host: Host,
    platform_id: str,
) -> None:
    device = Device(
        pack_id="appium-xcuitest",
        platform_id=platform_id,
        identity_scheme="apple_udid",
        identity_scope="global",
        identity_value=f"{platform_id}-probe-gate",
        connection_target=f"{platform_id}-probe-gate",
        name="Probe Gate Apple Device",
        os_version="26.4.2",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network if platform_id == "tvos" else ConnectionType.usb,
        verified_at=datetime.now(UTC),
    )

    with patch("app.services.node_health.is_ready_for_use_async", new_callable=AsyncMock, return_value=True):
        assert await _should_probe_node_health(db_session, device) is False


async def test_busy_node_uses_status_fallback(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="nh-008",
        connection_target="nh-008",
        name="Busy Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        verified_at=datetime.now(UTC),
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(device_id=device.id, port=4730, grid_url="http://hub:4444", state=NodeState.running)
    db_session.add(node)
    await db_session.commit()

    with (
        patch("app.services.node_health.fetch_appium_probe_session", new_callable=AsyncMock) as probe_mock,
        patch(
            "app.services.node_health.fetch_appium_status",
            new_callable=AsyncMock,
            return_value={"running": True, "port": 4730},
        ) as status_mock,
    ):
        await _check_nodes(db_session)

    probe_mock.assert_not_awaited()
    status_mock.assert_awaited_once()


async def test_virtual_node_uses_status_fallback(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="avd:Pixel_6",
        connection_target="Pixel_6",
        name="Pixel 6 Emulator",
        os_version="17",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.emulator,
        connection_type=ConnectionType.virtual,
        verified_at=datetime.now(UTC),
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(device_id=device.id, port=4733, grid_url="http://hub:4444", state=NodeState.running)
    db_session.add(node)
    await db_session.commit()

    with (
        patch("app.services.node_health.fetch_appium_probe_session", new_callable=AsyncMock) as probe_mock,
        patch(
            "app.services.node_health.fetch_appium_status",
            new_callable=AsyncMock,
            return_value={"running": True, "port": 4733},
        ) as status_mock,
    ):
        await _check_nodes(db_session)

    probe_mock.assert_not_awaited()
    status_mock.assert_awaited_once()


async def test_node_health_dispatches_checks_concurrently(db_session: AsyncSession, db_host: Host) -> None:
    first_device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="nh-009",
        connection_target="nh-009",
        name="Concurrent Phone 1",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        verified_at=datetime.now(UTC),
    )
    second_device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="nh-010",
        connection_target="nh-010",
        name="Concurrent Phone 2",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        verified_at=datetime.now(UTC),
    )
    db_session.add_all([first_device, second_device])
    await db_session.flush()

    first_node = AppiumNode(device_id=first_device.id, port=4731, grid_url="http://hub:4444", state=NodeState.running)
    second_node = AppiumNode(
        device_id=second_device.id,
        port=4732,
        grid_url="http://hub:4444",
        state=NodeState.running,
    )
    db_session.add_all([first_node, second_node])
    await db_session.commit()

    started_ports: set[int] = set()
    both_started = asyncio.Event()
    release_checks = asyncio.Event()

    async def fake_check_node_health(
        node: AppiumNode,
        device: Device,
        *,
        probe_capabilities: dict[str, object] | None = None,
    ) -> ProbeResult:
        _ = device, probe_capabilities
        started_ports.add(node.port)
        if len(started_ports) == 2:
            both_started.set()
        await both_started.wait()
        await release_checks.wait()
        return ProbeResult(status="ack")

    with (
        patch(
            "app.services.node_health.capability_service.get_device_capabilities",
            new_callable=AsyncMock,
            return_value={"platformName": "Android"},
        ),
        patch("app.services.node_health._check_node_health", side_effect=fake_check_node_health),
    ):
        task = asyncio.create_task(_check_nodes(db_session))
        await asyncio.wait_for(both_started.wait(), timeout=1)
        release_checks.set()
        await asyncio.wait_for(task, timeout=1)

    assert started_ports == {4731, 4732}


def _build_tristate_device(db_host: Host, identity: str) -> Device:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=identity,
        connection_target=identity,
        name=f"Tristate {identity}",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    device.host = db_host  # populate relationship for in-process require_management_host
    return device


async def test_check_node_health_returns_none_on_agent_unreachable(db_session: AsyncSession, db_host: Host) -> None:
    device = _build_tristate_device(db_host, "nh-tristate-1")
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(device_id=device.id, port=4730, grid_url="http://hub:4444", state=NodeState.running)

    with patch(
        "app.services.node_health.fetch_appium_status",
        AsyncMock(side_effect=AgentUnreachableError(db_host.ip, "boom")),
    ):
        result = await _check_node_health(node, device, probe_capabilities=None)

    assert result.status == "indeterminate"


async def test_check_node_health_returns_none_on_response_error(db_session: AsyncSession, db_host: Host) -> None:
    device = _build_tristate_device(db_host, "nh-tristate-2")
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(device_id=device.id, port=4731, grid_url="http://hub:4444", state=NodeState.running)

    with patch(
        "app.services.node_health.fetch_appium_status",
        AsyncMock(side_effect=AgentResponseError(db_host.ip, "boom", http_status=503)),
    ):
        result = await _check_node_health(node, device, probe_capabilities=None)

    assert result.status == "indeterminate"


async def test_check_node_health_returns_none_on_circuit_open(db_session: AsyncSession, db_host: Host) -> None:
    device = _build_tristate_device(db_host, "nh-tristate-3")
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(device_id=device.id, port=4732, grid_url="http://hub:4444", state=NodeState.running)

    with patch(
        "app.services.node_health.fetch_appium_status",
        AsyncMock(side_effect=CircuitOpenError(db_host.ip, retry_after_seconds=10.0)),
    ):
        result = await _check_node_health(node, device, probe_capabilities=None)

    assert result.status == "indeterminate"


async def test_check_node_health_returns_false_when_device_has_no_host(db_session: AsyncSession, db_host: Host) -> None:
    # Device with no host relationship → require_management_host raises NodeManagerError
    # Must surface as False (genuine misconfiguration, not reachability problem)
    device = _build_tristate_device(db_host, "nh-tristate-4")
    device.host = None
    device.host_id = None
    node = AppiumNode(device_id=None, port=4733, grid_url="http://hub:4444", state=NodeState.running)

    result = await _check_node_health(node, device, probe_capabilities=None)
    assert result.status == "refused"


async def test_check_node_health_returns_true_on_running_status(db_session: AsyncSession, db_host: Host) -> None:
    device = _build_tristate_device(db_host, "nh-tristate-5")
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(device_id=device.id, port=4734, grid_url="http://hub:4444", state=NodeState.running)

    with patch(
        "app.services.node_health.fetch_appium_status",
        AsyncMock(return_value={"running": True}),
    ):
        result = await _check_node_health(node, device, probe_capabilities=None)

    assert result.status == "ack"


async def test_check_node_health_status_path_returns_none_on_http_error(
    db_session: AsyncSession, db_host: Host
) -> None:
    """``appium_status`` returns ``None`` for non-2xx responses; that must be
    treated as indeterminate, not "not running"."""
    device = _build_tristate_device(db_host, "nh-tristate-http-status")
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(device_id=device.id, port=4735, grid_url="http://hub:4444", state=NodeState.running)

    with patch(
        "app.services.node_health.fetch_appium_status",
        AsyncMock(return_value=None),
    ):
        result = await _check_node_health(node, device, probe_capabilities=None)

    assert result.status == "indeterminate"


async def test_check_node_health_probe_path_returns_none_on_http_error(db_session: AsyncSession, db_host: Host) -> None:
    """``appium_probe_session`` returns ``(False, "Probe session failed (HTTP <code>)")``
    on non-2xx responses; that must be treated as indeterminate, not as a
    confirmed unhealthy probe."""
    device = _build_tristate_device(db_host, "nh-tristate-http-probe")
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(device_id=device.id, port=4736, grid_url="http://hub:4444", state=NodeState.running)

    with patch(
        "app.services.node_health.fetch_appium_probe_session",
        AsyncMock(return_value=(False, "Probe session failed (HTTP 503)")),
    ):
        result = await _check_node_health(node, device, probe_capabilities={"platformName": "Android"})

    assert result.status == "indeterminate"


async def test_check_node_health_probe_path_returns_false_on_genuine_failure(
    db_session: AsyncSession, db_host: Host
) -> None:
    """A definitive probe failure (Appium-side) keeps the False result so the
    node_health loop still records it and eventually escalates."""
    device = _build_tristate_device(db_host, "nh-tristate-probe-fail")
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(device_id=device.id, port=4737, grid_url="http://hub:4444", state=NodeState.running)

    with patch(
        "app.services.node_health.fetch_appium_probe_session",
        AsyncMock(return_value=(False, "Probe session returned an invalid payload")),
    ):
        result = await _check_node_health(node, device, probe_capabilities={"platformName": "Android"})

    assert result.status == "refused"


async def test_indeterminate_probe_does_not_flip_columns_or_counter(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="nh-indet-1",
        connection_target="nh-indet-1",
        name="Indeterminate Phone",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(device_id=device.id, port=4750, grid_url="http://hub:4444", state=NodeState.running)
    db_session.add(node)
    await db_session.commit()

    # Pre-set projected node health to known-healthy.
    await device_health.apply_node_state_transition(
        db_session,
        device,
        new_state=NodeState.running,
        health_running=None,
        health_state=None,
        mark_offline=False,
    )
    await db_session.commit()

    with patch("app.services.node_health._check_node_health", return_value=ProbeResult(status="indeterminate")):
        await _check_nodes(db_session)

    # Counter unchanged (still absent)
    assert str(node.id) not in await get_node_health_control_plane_state(db_session)

    # Column projection still healthy.
    await db_session.refresh(device, attribute_names=["appium_node"])
    assert device.appium_node is not None
    assert device.appium_node.state == NodeState.running
    assert device.appium_node.health_running is None
    assert device_health.build_public_summary(device)["healthy"] is True

    # Device still available
    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.available


async def test_per_host_probe_concurrency_capped(db_session: AsyncSession, db_host: Host) -> None:
    devices: list[Device] = []
    nodes: list[AppiumNode] = []
    for index in range(6):
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value=f"nh-conc-{index}",
            connection_target=f"nh-conc-{index}",
            name=f"Concurrency Phone {index}",
            os_version="14",
            host_id=db_host.id,
            availability_status=DeviceAvailabilityStatus.available,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
        db_session.add(device)
        await db_session.flush()
        node = AppiumNode(
            device_id=device.id,
            port=4760 + index,
            grid_url="http://hub:4444",
            state=NodeState.running,
        )
        db_session.add(node)
        devices.append(device)
        nodes.append(node)
    await db_session.commit()

    in_flight = 0
    peak = 0

    async def slow_probe(*_args: object, **_kwargs: object) -> ProbeResult:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.05)
            return ProbeResult(status="ack")
        finally:
            in_flight -= 1

    with patch("app.services.node_health._check_node_health", side_effect=slow_probe):
        await _check_nodes(db_session)

    assert peak <= 2, f"per-host probe concurrency exceeded cap: peak={peak}"


async def test_node_health_recovery_clears_pending_stop(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="nh-recovery-clears-pending",
        connection_target="nh-recovery-clears-pending",
        name="Recovery Clears Pending",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        lifecycle_policy_state={
            "stop_pending": True,
            "stop_pending_reason": "Probe failed",
            "stop_pending_since": "2026-05-04T10:00:00+00:00",
            "last_action": "auto_stop_deferred",
            "last_failure_source": "node_health",
            "last_failure_reason": "Probe failed",
            "recovery_suppressed_reason": None,
        },
    )
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(device_id=device.id, port=4780, grid_url="http://hub:4444", state=NodeState.running)
    db_session.add(node)
    await db_session.commit()

    # Seed prior failure state so recovery branch fires.
    await set_node_health_failure_count(db_session, str(node.id), 1)
    await device_health.apply_node_state_transition(
        db_session,
        device,
        health_running=False,
        health_state="error",
        mark_offline=False,
    )
    await db_session.commit()

    with patch("app.services.node_health._check_node_health", return_value=ProbeResult(status="ack")):
        await _check_nodes(db_session)

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False

    incidents = list(
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == device.id, DeviceEvent.event_type == DeviceEventType.lifecycle_recovered
                )
            )
        )
        .scalars()
        .all()
    )
    # Audit P2: the dedicated node-health ``lifecycle_recovered`` event below
    # is the canonical recovery audit entry. ``clear_pending_auto_stop_on_recovery``
    # is invoked with ``record_incident=False`` so the recovery moment shows up
    # exactly once instead of twice on the device timeline.
    assert len(incidents) == 1
    detail = (incidents[0].details or {}).get("detail") or ""
    assert "resumed healthy operation" in detail.lower()
