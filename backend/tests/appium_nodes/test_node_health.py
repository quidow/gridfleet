import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import select

from app.agent_comm.probe_result import ProbeResult
from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import node_health
from app.appium_nodes.services.node_health import NodeHealthService
from app.core.errors import AgentResponseError, AgentUnreachableError, CircuitOpenError
from app.devices import locking as device_locking
from app.devices.models import (
    ConnectionType,
    Device,
    DeviceEvent,
    DeviceEventType,
    DeviceIntent,
    DeviceOperationalState,
    DeviceType,
)
from app.devices.services import health as device_health
from app.devices.services.health import DeviceHealthService
from app.devices.services.lifecycle_policy_state import now, write_state
from app.devices.services.lifecycle_policy_state import state as policy_state
from app.hosts.models import Host, HostStatus
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.runs.service_reservation import RunReservationService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _make_real_recovery_control(publisher: object = None) -> LifecyclePolicyService:
    """Return a real LifecyclePolicyService for tests that need actual DB mutations."""
    pub = publisher if publisher is not None else event_bus
    return LifecyclePolicyService(
        review=build_review_service(),
        publisher=pub,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=pub,
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
        viability=Mock(),
        node_manager=AsyncMock(),
    )


async def set_node_health_failure_count(db_session: AsyncSession, node_key: str, count: int) -> None:
    node = await db_session.get(AppiumNode, uuid.UUID(node_key))
    assert node is not None
    node.consecutive_health_failures = count
    await db_session.commit()


async def get_node_health_control_plane_state(db_session: AsyncSession) -> dict[str, int]:
    nodes = (await db_session.execute(select(AppiumNode))).scalars().all()
    return {str(node.id): node.consecutive_health_failures for node in nodes if node.consecutive_health_failures > 0}


async def _seed_policy_state(db_session: AsyncSession, device_id: uuid.UUID, **keys: object) -> None:
    locked = await device_locking.lock_device(db_session, device_id)
    state = policy_state(locked)
    state.update(keys)
    write_state(locked, state)
    await db_session.commit()


async def _auto_recovery_intents(db_session: AsyncSession, device_id: uuid.UUID) -> list[DeviceIntent]:
    rows = (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device_id))).scalars().all()
    return [row for row in rows if row.source.startswith("auto_recovery:")]


async def _running_node_fixture(
    db_session: AsyncSession,
    db_host: Host,
    *,
    name: str,
    identity: str,
    port: int,
) -> tuple[Device, AppiumNode]:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=identity,
        connection_target=identity,
        name=name,
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=port,
        desired_state=AppiumDesiredState.running,
        desired_port=port,
        pid=1,
        active_connection_target="target",
    )
    db_session.add(node)
    await db_session.commit()
    await set_node_health_failure_count(db_session, str(node.id), 2)
    return device, node


async def _drive_refused_node_health(
    db_session: AsyncSession, host_id: uuid.UUID, *, settings: FakeSettingsReader
) -> None:
    with patch.object(NodeHealthService, "_check_node_health", return_value=ProbeResult(status="refused")):
        await NodeHealthService(
            publisher=Mock(),
            settings=settings,
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=AsyncMock(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=AsyncMock(),
        ).check_host_nodes(db_session, host_id=host_id)


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
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=1,
        active_connection_target="target",
    )
    db_session.add(node)
    await db_session.commit()

    # Pre-set some failure counts
    await set_node_health_failure_count(db_session, str(node.id), 2)

    with (
        patch.object(NodeHealthService, "_check_node_health", return_value=ProbeResult(status="ack")),
    ):
        svc = NodeHealthService(
            publisher=event_bus,
            settings=FakeSettingsReader(
                {
                    "general.node_max_failures": 3,
                    "appium_reconciler.restart_window_sec": 300,
                    "appium.startup_timeout_sec": 30,
                }
            ),
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=AsyncMock(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=AsyncMock(),
        )
        await svc.check_host_nodes(db_session, host_id=device.host_id)

    assert str(node.id) not in await get_node_health_control_plane_state(db_session)
    await db_session.refresh(node)
    assert node.observed_running
    # A successful direct probe persists the positive health signal truthfully
    # instead of clearing the columns to NULL (the post-cutover regression).
    assert node.health_running is True
    assert node.health_state is None
    assert node.last_health_checked_at is not None


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
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4724,
        desired_state=AppiumDesiredState.running,
        desired_port=4724,
        pid=1,
        active_connection_target="target",
    )
    db_session.add(node)
    await db_session.commit()

    with (
        patch.object(NodeHealthService, "_check_node_health", return_value=ProbeResult(status="refused")),
    ):
        svc = NodeHealthService(
            publisher=event_bus,
            settings=FakeSettingsReader(
                {
                    "general.node_max_failures": 3,
                    "appium_reconciler.restart_window_sec": 300,
                    "appium.startup_timeout_sec": 30,
                }
            ),
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=AsyncMock(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=AsyncMock(),
        )
        await svc.check_host_nodes(db_session, host_id=device.host_id)

    assert (await get_node_health_control_plane_state(db_session))[str(node.id)] == 1
    await db_session.refresh(node)
    assert node.observed_running is True  # Not yet at max
    assert node.health_state == "error"
    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available


async def test_node_restart_via_agent_on_max_failures(db_session: AsyncSession) -> None:
    """Node with a host writes restart intent on max failures."""
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
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4726,
        desired_state=AppiumDesiredState.running,
        desired_port=4726,
        pid=1,
        active_connection_target="target",
    )
    db_session.add(node)
    await db_session.commit()

    await set_node_health_failure_count(db_session, str(node.id), 2)

    with (
        patch.object(NodeHealthService, "_check_node_health", return_value=ProbeResult(status="refused")),
    ):
        await NodeHealthService(
            publisher=Mock(),
            settings=FakeSettingsReader(
                {
                    "general.node_max_failures": 3,
                    "appium_reconciler.restart_window_sec": 300,
                    "appium.startup_timeout_sec": 30,
                }
            ),
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=AsyncMock(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=AsyncMock(),
        ).check_host_nodes(db_session, host_id=device.host_id)

    await db_session.refresh(node)
    assert node.observed_running is True
    assert node.health_state == "error"
    assert node.desired_state == AppiumDesiredState.running
    assert node.transition_token is not None
    assert str(node.id) not in await get_node_health_control_plane_state(db_session)


async def test_restart_deferred_while_shared_backoff_armed(db_session: AsyncSession, db_host: Host) -> None:
    """Reaching node_max_failures during an armed backoff must not restart."""
    device, node = await _running_node_fixture(
        db_session,
        db_host,
        name="Deferred Backoff Phone",
        identity="nh-backoff-deferred",
        port=4790,
    )
    await _seed_policy_state(
        db_session,
        device.id,
        backoff_until=(now() + timedelta(seconds=600)).isoformat(),
    )

    await _drive_refused_node_health(
        db_session,
        device.host_id,
        settings=FakeSettingsReader({"general.node_max_failures": 3, "appium_reconciler.restart_window_sec": 300}),
    )

    assert await _auto_recovery_intents(db_session, device.id) == []
    await db_session.refresh(node)
    assert node.transition_token is None


async def test_restart_counts_toward_shared_ladder(db_session: AsyncSession, db_host: Host) -> None:
    """Each triggered restart records one remediation failure."""
    device, _node = await _running_node_fixture(
        db_session,
        db_host,
        name="Counted Restart Phone",
        identity="nh-counted-restart",
        port=4791,
    )

    await _drive_refused_node_health(
        db_session,
        device.host_id,
        settings=FakeSettingsReader({"general.node_max_failures": 3, "appium_reconciler.restart_window_sec": 300}),
    )

    assert len(await _auto_recovery_intents(db_session, device.id)) == 2
    refreshed = await db_session.get(Device, device.id)
    assert refreshed is not None
    state = policy_state(refreshed)
    assert state["recovery_backoff_attempts"] == 1
    assert state["backoff_until"] is not None
    assert state["last_failure_source"] == "node_health"


async def test_restart_shelves_at_review_threshold(db_session: AsyncSession, db_host: Host) -> None:
    """At the shared threshold the device is shelved instead of restarted."""
    device, _node = await _running_node_fixture(
        db_session,
        db_host,
        name="Shelved Restart Phone",
        identity="nh-shelved-restart",
        port=4792,
    )
    await _seed_policy_state(db_session, device.id, recovery_backoff_attempts=4)

    await _drive_refused_node_health(
        db_session,
        device.host_id,
        settings=FakeSettingsReader({"general.node_max_failures": 3, "appium_reconciler.restart_window_sec": 300}),
    )

    refreshed = await db_session.get(Device, device.id)
    assert refreshed is not None
    assert refreshed.review_required is True
    assert await _auto_recovery_intents(db_session, device.id) == []


async def test_node_restart_intent_marks_device_offline_until_reconciler_recovers(db_session: AsyncSession) -> None:
    """Repeated health failures mark the device offline and queue restart intent."""
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
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4727,
        desired_state=AppiumDesiredState.running,
        desired_port=4727,
        pid=1,
        active_connection_target="target",
    )
    db_session.add(node)
    await db_session.commit()

    await set_node_health_failure_count(db_session, str(node.id), 2)

    with (
        patch.object(NodeHealthService, "_check_node_health", return_value=ProbeResult(status="refused")),
    ):
        await NodeHealthService(
            publisher=Mock(),
            settings=FakeSettingsReader(
                {
                    "general.node_max_failures": 3,
                    "appium_reconciler.restart_window_sec": 300,
                    "appium.startup_timeout_sec": 30,
                }
            ),
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=AsyncMock(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=AsyncMock(),
        ).check_host_nodes(db_session, host_id=device.host_id)

    await db_session.refresh(node)
    assert node.observed_running is True
    assert node.health_state == "error"
    assert node.desired_state == AppiumDesiredState.running
    assert node.transition_token is not None
    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline


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
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4728,
        desired_state=AppiumDesiredState.running,
        desired_port=4728,
        pid=1,
        active_connection_target="target",
    )
    db_session.add(node)
    await db_session.commit()

    node_key = str(node.id)
    await set_node_health_failure_count(db_session, str(node.id), 2)

    with (
        patch(
            "app.appium_nodes.services.node_health.require_management_host",
            side_effect=NodeManagerError("Device management host invariant is broken"),
        ),
    ):
        await NodeHealthService(
            publisher=Mock(),
            settings=FakeSettingsReader(
                {
                    "general.node_max_failures": 3,
                    "appium_reconciler.restart_window_sec": 300,
                    "appium.startup_timeout_sec": 30,
                }
            ),
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=AsyncMock(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=AsyncMock(),
        ).check_host_nodes(db_session, host_id=device.host_id)

    await db_session.refresh(node)
    assert node.observed_running is True
    assert node.desired_state == AppiumDesiredState.running
    assert node.transition_token is not None
    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline
    assert node_key not in await get_node_health_control_plane_state(db_session)


async def test_available_verified_node_uses_status_check(db_session: AsyncSession, db_host: Host) -> None:
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
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        verified_at=datetime.now(UTC),
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4729,
        desired_state=AppiumDesiredState.running,
        desired_port=4729,
        pid=1,
        active_connection_target="target",
    )
    db_session.add(node)
    await db_session.commit()

    with (
        patch(
            "app.appium_nodes.services.node_health.fetch_appium_status",
            new_callable=AsyncMock,
            return_value={"running": True, "port": 4729},
        ) as status_mock,
    ):
        await NodeHealthService(
            publisher=event_bus,
            settings=FakeSettingsReader(
                {
                    "general.node_max_failures": 3,
                    "appium_reconciler.restart_window_sec": 300,
                    "appium.startup_timeout_sec": 30,
                }
            ),
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=AsyncMock(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=AsyncMock(),
        ).check_host_nodes(db_session, host_id=device.host_id)

    status_mock.assert_awaited_once()


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
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        verified_at=datetime.now(UTC),
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4734,
        desired_state=AppiumDesiredState.running,
        desired_port=4734,
        pid=1,
        active_connection_target="target",
    )
    db_session.add(node)
    await db_session.commit()

    with (
        patch(
            "app.appium_nodes.services.node_health.fetch_appium_status",
            new_callable=AsyncMock,
            return_value={"running": True, "port": 4734},
        ) as status_mock,
    ):
        await NodeHealthService(
            publisher=event_bus,
            settings=FakeSettingsReader(
                {
                    "general.node_max_failures": 3,
                    "appium_reconciler.restart_window_sec": 300,
                    "appium.startup_timeout_sec": 30,
                }
            ),
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=AsyncMock(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=AsyncMock(),
        ).check_host_nodes(db_session, host_id=device.host_id)

    status_mock.assert_awaited_once()


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
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        verified_at=datetime.now(UTC),
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4730,
        desired_state=AppiumDesiredState.running,
        desired_port=4730,
        pid=1,
        active_connection_target="target",
    )
    db_session.add(node)
    await db_session.commit()

    with (
        patch(
            "app.appium_nodes.services.node_health.fetch_appium_status",
            new_callable=AsyncMock,
            return_value={"running": True, "port": 4730},
        ) as status_mock,
    ):
        await NodeHealthService(
            publisher=event_bus,
            settings=FakeSettingsReader(
                {
                    "general.node_max_failures": 3,
                    "appium_reconciler.restart_window_sec": 300,
                    "appium.startup_timeout_sec": 30,
                }
            ),
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=AsyncMock(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=AsyncMock(),
        ).check_host_nodes(db_session, host_id=device.host_id)

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
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.emulator,
        connection_type=ConnectionType.virtual,
        verified_at=datetime.now(UTC),
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4733,
        desired_state=AppiumDesiredState.running,
        desired_port=4733,
        pid=1,
        active_connection_target="target",
    )
    db_session.add(node)
    await db_session.commit()

    with (
        patch(
            "app.appium_nodes.services.node_health.fetch_appium_status",
            new_callable=AsyncMock,
            return_value={"running": True, "port": 4733},
        ) as status_mock,
    ):
        await NodeHealthService(
            publisher=event_bus,
            settings=FakeSettingsReader(
                {
                    "general.node_max_failures": 3,
                    "appium_reconciler.restart_window_sec": 300,
                    "appium.startup_timeout_sec": 30,
                }
            ),
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=AsyncMock(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=AsyncMock(),
        ).check_host_nodes(db_session, host_id=device.host_id)

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
        operational_state=DeviceOperationalState.available,
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
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        verified_at=datetime.now(UTC),
    )
    db_session.add_all([first_device, second_device])
    await db_session.flush()

    first_node = AppiumNode(
        device_id=first_device.id,
        port=4731,
        desired_state=AppiumDesiredState.running,
        desired_port=4731,
        pid=1,
        active_connection_target="target",
    )
    second_node = AppiumNode(
        device_id=second_device.id,
        port=4732,
        desired_state=AppiumDesiredState.running,
        desired_port=4732,
        pid=1,
        active_connection_target="target",
    )
    db_session.add_all([first_node, second_node])
    await db_session.commit()

    started_ports: set[int] = set()
    both_started = asyncio.Event()
    release_checks = asyncio.Event()

    async def fake_check_node_health(node: AppiumNode, device: Device) -> ProbeResult:
        _ = device
        started_ports.add(node.port)
        if len(started_ports) == 2:
            both_started.set()
        await both_started.wait()
        await release_checks.wait()
        return ProbeResult(status="ack")

    with (
        patch.object(NodeHealthService, "_check_node_health", side_effect=fake_check_node_health),
    ):
        task = asyncio.create_task(
            NodeHealthService(
                publisher=event_bus,
                settings=FakeSettingsReader(
                    {
                        "general.node_max_failures": 3,
                        "appium_reconciler.restart_window_sec": 300,
                        "appium.startup_timeout_sec": 30,
                    }
                ),
                pool=Mock(),
                circuit_breaker=Mock(),
                recovery_control=AsyncMock(),
                health=DeviceHealthService(publisher=event_bus),
                incidents=AsyncMock(),
            ).check_host_nodes(db_session, host_id=db_host.id)
        )
        # Generous budgets: serial dispatch would block both waits forever (the second
        # check never starts, so both_started never sets), so a real regression still
        # trips these. 1s was too tight for a loaded CI runner doing real DB I/O — the
        # task loads nodes/devices and commits health state — and flaked intermittently.
        await asyncio.wait_for(both_started.wait(), timeout=5.0)
        release_checks.set()
        await asyncio.wait_for(task, timeout=5.0)

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
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    device.host = db_host  # populate relationship for in-process require_management_host
    return device


async def test_check_node_health_returns_none_on_agent_unreachable(db_session: AsyncSession, db_host: Host) -> None:
    device = _build_tristate_device(db_host, "nh-tristate-1")
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(
        device_id=device.id,
        port=4730,
        desired_state=AppiumDesiredState.running,
        desired_port=4730,
        pid=1,
        active_connection_target="target",
    )

    with patch(
        "app.appium_nodes.services.node_health.fetch_appium_status",
        AsyncMock(side_effect=AgentUnreachableError(db_host.ip, "boom")),
    ):
        result = await NodeHealthService(
            publisher=Mock(),
            settings=FakeSettingsReader({}),
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=AsyncMock(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=AsyncMock(),
        )._check_node_health(node, device)

    assert result.status == "indeterminate"


async def test_check_node_health_returns_none_on_response_error(db_session: AsyncSession, db_host: Host) -> None:
    device = _build_tristate_device(db_host, "nh-tristate-2")
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(
        device_id=device.id,
        port=4731,
        desired_state=AppiumDesiredState.running,
        desired_port=4731,
        pid=1,
        active_connection_target="target",
    )

    with patch(
        "app.appium_nodes.services.node_health.fetch_appium_status",
        AsyncMock(side_effect=AgentResponseError(db_host.ip, "boom", http_status=503)),
    ):
        result = await NodeHealthService(
            publisher=Mock(),
            settings=FakeSettingsReader({}),
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=AsyncMock(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=AsyncMock(),
        )._check_node_health(node, device)

    assert result.status == "indeterminate"


async def test_check_node_health_returns_none_on_circuit_open(db_session: AsyncSession, db_host: Host) -> None:
    device = _build_tristate_device(db_host, "nh-tristate-3")
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(
        device_id=device.id,
        port=4732,
        desired_state=AppiumDesiredState.running,
        desired_port=4732,
        pid=1,
        active_connection_target="target",
    )

    with patch(
        "app.appium_nodes.services.node_health.fetch_appium_status",
        AsyncMock(side_effect=CircuitOpenError(db_host.ip, retry_after_seconds=10.0)),
    ):
        result = await NodeHealthService(
            publisher=Mock(),
            settings=FakeSettingsReader({}),
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=AsyncMock(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=AsyncMock(),
        )._check_node_health(node, device)

    assert result.status == "indeterminate"


async def test_check_node_health_returns_false_when_device_has_no_host(db_session: AsyncSession, db_host: Host) -> None:
    # Device with no host relationship → require_management_host raises NodeManagerError
    # Must surface as False (genuine misconfiguration, not reachability problem)
    device = _build_tristate_device(db_host, "nh-tristate-4")
    device.host = None
    device.host_id = None
    node = AppiumNode(
        device_id=None,
        port=4733,
        desired_state=AppiumDesiredState.running,
        desired_port=4733,
        pid=1,
        active_connection_target="target",
    )

    result = await NodeHealthService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        recovery_control=AsyncMock(),
        health=DeviceHealthService(publisher=event_bus),
        incidents=AsyncMock(),
    )._check_node_health(node, device)
    assert result.status == "refused"


async def test_check_node_health_returns_true_on_running_status(db_session: AsyncSession, db_host: Host) -> None:
    device = _build_tristate_device(db_host, "nh-tristate-5")
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(
        device_id=device.id,
        port=4734,
        desired_state=AppiumDesiredState.running,
        desired_port=4734,
        pid=1,
        active_connection_target="target",
    )

    with patch(
        "app.appium_nodes.services.node_health.fetch_appium_status",
        AsyncMock(return_value={"running": True}),
    ):
        result = await NodeHealthService(
            publisher=Mock(),
            settings=FakeSettingsReader({}),
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=AsyncMock(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=AsyncMock(),
        )._check_node_health(node, device)

    assert result.status == "ack"


async def test_check_node_health_status_path_returns_none_on_http_error(
    db_session: AsyncSession, db_host: Host
) -> None:
    """``appium_status`` returns ``None`` for non-2xx responses; that must be
    treated as indeterminate, not "not running"."""
    device = _build_tristate_device(db_host, "nh-tristate-http-status")
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(
        device_id=device.id,
        port=4735,
        desired_state=AppiumDesiredState.running,
        desired_port=4735,
        pid=1,
        active_connection_target="target",
    )

    with patch(
        "app.appium_nodes.services.node_health.fetch_appium_status",
        AsyncMock(return_value=None),
    ):
        result = await NodeHealthService(
            publisher=Mock(),
            settings=FakeSettingsReader({}),
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=AsyncMock(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=AsyncMock(),
        )._check_node_health(node, device)

    assert result.status == "indeterminate"


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
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4750,
        desired_state=AppiumDesiredState.running,
        desired_port=4750,
        pid=1,
        active_connection_target="target",
    )
    db_session.add(node)
    await db_session.commit()

    # Pre-set projected node health to known-healthy.
    await DeviceHealthService(publisher=event_bus).apply_node_state_transition(
        db_session,
        device,
        health_running=None,
        health_state=None,
        mark_offline=False,
    )
    await db_session.commit()

    with (
        patch.object(NodeHealthService, "_check_node_health", return_value=ProbeResult(status="indeterminate")),
    ):
        await NodeHealthService(
            publisher=event_bus,
            settings=FakeSettingsReader(
                {
                    "general.node_max_failures": 3,
                    "appium_reconciler.restart_window_sec": 300,
                    "appium.startup_timeout_sec": 30,
                }
            ),
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=AsyncMock(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=AsyncMock(),
        ).check_host_nodes(db_session, host_id=device.host_id)

    # Counter unchanged (still absent)
    assert str(node.id) not in await get_node_health_control_plane_state(db_session)

    # Column projection still healthy.
    await db_session.refresh(device, attribute_names=["appium_node"])
    assert device.appium_node is not None
    assert device.appium_node.observed_running
    assert device.appium_node.health_running is None
    assert device_health.build_public_summary(device)["node"]["status"] == "ok"

    # Device still available
    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available


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
            operational_state=DeviceOperationalState.available,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
        db_session.add(device)
        await db_session.flush()
        node = AppiumNode(
            device_id=device.id,
            port=4760 + index,
            desired_state=AppiumDesiredState.running,
            desired_port=4760,
            pid=1,
            active_connection_target="target",
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

    with (
        patch.object(NodeHealthService, "_check_node_health", side_effect=slow_probe),
    ):
        await NodeHealthService(
            publisher=event_bus,
            settings=FakeSettingsReader(
                {
                    "general.node_max_failures": 3,
                    "general.probe_concurrency_per_host": 2,
                    "appium_reconciler.restart_window_sec": 300,
                    "appium.startup_timeout_sec": 30,
                }
            ),
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=AsyncMock(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=AsyncMock(),
        ).check_host_nodes(db_session, host_id=device.host_id)

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
        operational_state=DeviceOperationalState.available,
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
    node = AppiumNode(
        device_id=device.id,
        port=4780,
        desired_state=AppiumDesiredState.running,
        desired_port=4780,
        pid=1,
        active_connection_target="target",
    )
    db_session.add(node)
    await db_session.commit()

    # Seed prior failure state so recovery branch fires.
    await set_node_health_failure_count(db_session, str(node.id), 1)
    await DeviceHealthService(publisher=event_bus).apply_node_state_transition(
        db_session,
        device,
        health_running=False,
        health_state="error",
        mark_offline=False,
    )
    await db_session.commit()

    with (
        patch.object(NodeHealthService, "_check_node_health", return_value=ProbeResult(status="ack")),
    ):
        await NodeHealthService(
            publisher=event_bus,
            settings=FakeSettingsReader(
                {
                    "general.node_max_failures": 3,
                    "appium_reconciler.restart_window_sec": 300,
                    "appium.startup_timeout_sec": 30,
                }
            ),
            pool=Mock(),
            circuit_breaker=Mock(),
            recovery_control=_make_real_recovery_control(),
            health=DeviceHealthService(publisher=event_bus),
            incidents=LifecycleIncidentService(),
        ).check_host_nodes(db_session, host_id=device.host_id)

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


async def test_process_node_health_early_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    device = Device(
        id=uuid.uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="nh-early",
        connection_target="nh-early",
        name="Node Health Early",
        os_version="14",
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db = AsyncMock()
    svc = NodeHealthService(
        publisher=event_bus,
        settings=FakeSettingsReader({"general.node_max_failures": 3, "appium.startup_timeout_sec": 30}),
        pool=Mock(),
        circuit_breaker=Mock(),
        recovery_control=AsyncMock(),
        health=DeviceHealthService(publisher=event_bus),
        incidents=AsyncMock(),
    )

    monkeypatch.setattr(node_health.appium_node_locking, "lock_appium_node_for_device", AsyncMock(return_value=None))
    _node_null = AppiumNode(device_id=device.id, port=4723)
    await svc._process_node_health(
        db,
        _node_null,
        device,
        result=ProbeResult(status="ack"),
    )

    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=1,
        active_connection_target="old",
    )
    monkeypatch.setattr(node_health.appium_node_locking, "lock_appium_node_for_device", AsyncMock(return_value=node))
    await svc._process_node_health(
        db,
        node,
        device,
        result=ProbeResult(status="ack"),
        observed_port=4724,
        observed_pid=1,
        observed_active_connection_target="old",
    )

    node.pid = None
    await svc._process_node_health(
        db,
        node,
        device,
        result=ProbeResult(status="ack"),
    )

    node.pid = 1
    await svc._process_node_health(
        db,
        node,
        device,
        result=ProbeResult(status="indeterminate"),
    )
