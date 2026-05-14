from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core.errors import AgentCallError
from app.devices.models import ConnectionType, Device, DeviceHold, DeviceOperationalState, DeviceType
from app.devices.services.connectivity import (
    _check_connectivity,
    _get_agent_devices,
    _get_lifecycle_state,
    get_connectivity_control_plane_state,
    track_previously_offline_device,
)
from app.hosts.models import Host, HostStatus

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


@pytest.fixture(autouse=True)
def _skip_lifecycle_state_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.devices.services.connectivity._get_lifecycle_state", AsyncMock(return_value=None))


@pytest.fixture(autouse=True)
def _noop_assert_current_leader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.devices.services.connectivity.assert_current_leader", AsyncMock(return_value=None))


async def _setup_host_and_device(
    db_session: AsyncSession,
    connection_target: str = "dc-001",
    device_operational_state: DeviceOperationalState = DeviceOperationalState.available,
    device_hold: DeviceHold | None = None,
    with_node: bool = False,
) -> tuple[Host, Device, AppiumNode | None]:
    host = Host(hostname="dc-host", ip="10.0.0.10", os_type="linux", agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=connection_target,
        connection_target=connection_target,
        name="Test Phone",
        os_version="14",
        host_id=host.id,
        operational_state=device_operational_state,
        hold=device_hold,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = None
    if with_node:
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=0,
            active_connection_target="",
        )
        db_session.add(node)

    await db_session.commit()
    return host, device, node


async def test_get_agent_devices_handles_malformed_candidates_and_unreachable_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = Host(hostname="dc-agent-host", ip="10.0.0.10", os_type="linux", agent_port=5100, status=HostStatus.online)
    monkeypatch.setattr(
        "app.devices.services.connectivity.get_pack_devices",
        AsyncMock(return_value={"candidates": "not-a-list"}),
    )
    assert await _get_agent_devices(host) == set()

    monkeypatch.setattr(
        "app.devices.services.connectivity.get_pack_devices",
        AsyncMock(
            return_value={
                "candidates": [
                    "bad",
                    {"identity_value": "serial-1", "detected_properties": "bad"},
                    {
                        "identity_value": "serial-2",
                        "detected_properties": {"connection_target": "10.0.0.5:5555"},
                    },
                ]
            }
        ),
    )
    aliases = await _get_agent_devices(host)
    assert aliases is not None
    assert {"serial-1", "serial-2", "10.0.0.5:5555"} <= aliases

    monkeypatch.setattr(
        "app.devices.services.connectivity.get_pack_devices",
        AsyncMock(side_effect=AgentCallError("10.0.0.10", "down")),
    )
    assert await _get_agent_devices(host) is None


async def test_connected_device_stays_available(db_session: AsyncSession) -> None:
    _host, device, _ = await _setup_host_and_device(db_session)

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
    ):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available


async def test_endpoint_only_device_stays_available_when_health_passes(db_session: AsyncSession) -> None:
    host, device, _ = await _setup_host_and_device(
        db_session,
        connection_target="192.168.1.50",
    )
    device.pack_id = "appium-roku-dlenroc"
    device.platform_id = "roku_network"
    device.identity_scheme = "roku_serial"
    device.identity_scope = "global"
    device.identity_value = "YJ1234567890"
    device.connection_type = ConnectionType.network
    device.ip_address = "192.168.1.50"
    device.device_config = {"roku_password": "secret"}
    await db_session.commit()

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True, "checks": [{"check_id": "ecp", "ok": True}]},
        ) as health,
    ):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available
    health.assert_awaited_once()
    assert host.status == HostStatus.online


async def test_endpoint_only_offline_device_auto_starts_when_health_passes(db_session: AsyncSession) -> None:
    _host, device, _ = await _setup_host_and_device(
        db_session,
        connection_target="192.168.1.50",
        device_operational_state=DeviceOperationalState.offline,
    )
    device.pack_id = "appium-roku-dlenroc"
    device.platform_id = "roku_network"
    device.identity_scheme = "roku_serial"
    device.identity_scope = "global"
    device.identity_value = "YJ1234567890"
    device.connection_type = ConnectionType.network
    device.ip_address = "192.168.1.50"
    device.device_config = {"roku_password": "secret"}
    await db_session.commit()

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True, "checks": [{"check_id": "ecp", "ok": True}]},
        ),
        patch(
            "app.devices.services.connectivity.lifecycle_policy.attempt_auto_recovery",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_recover,
    ):
        await _check_connectivity(db_session)

    mock_recover.assert_called_once()
    _, kwargs = mock_recover.call_args
    assert kwargs["reason"] == "Startup recovery after healthy endpoint check"
    assert "YJ1234567890" not in await get_connectivity_control_plane_state(db_session)


async def test_endpoint_health_branch_handles_top_level_failure_and_ip_ping_hysteresis(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host, failing, _ = await _setup_host_and_device(db_session, connection_target="192.168.1.51")
    failing.identity_value = "endpoint-failing"
    failing.connection_type = ConnectionType.network
    failing.ip_address = "192.168.1.51"
    ping_miss = Device(
        pack_id="appium-roku-dlenroc",
        platform_id="roku_network",
        identity_scheme="roku_serial",
        identity_scope="global",
        identity_value="endpoint-ping-miss",
        connection_target="192.168.1.52",
        name="Endpoint Ping Miss",
        os_version="14",
        host_id=_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        ip_address="192.168.1.52",
    )
    db_session.add(ping_miss)
    await db_session.commit()

    _stub_settings(monkeypatch, threshold=2, timeout=2.0, count=1)
    monkeypatch.setattr("app.devices.services.connectivity._uses_endpoint_health", AsyncMock(return_value=True))
    monkeypatch.setattr("app.devices.services.connectivity._get_agent_devices", AsyncMock(return_value=set()))

    async def endpoint_health(device: Device, **_kwargs: object) -> dict[str, object]:
        if device.identity_value == "endpoint-failing":
            return {"healthy": False}
        return healthy_payload(adb=True, ip_ping=False)

    monkeypatch.setattr("app.devices.services.connectivity._get_device_health", endpoint_health)
    monkeypatch.setattr(
        "app.devices.services.connectivity.lifecycle_policy.handle_health_failure",
        AsyncMock(),
    )

    await _check_connectivity(db_session)

    await db_session.refresh(failing)
    await db_session.refresh(ping_miss)
    assert failing.device_checks_healthy is False
    assert ping_miss.device_checks_healthy is True
    assert ping_miss.device_checks_summary == "Healthy (ip_ping miss 1/2)"


async def test_endpoint_offline_recovery_skip_and_failure_branches(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host, not_ready, _ = await _setup_host_and_device(
        db_session,
        connection_target="192.168.1.53",
        device_operational_state=DeviceOperationalState.offline,
    )
    not_ready.identity_value = "endpoint-not-ready"
    not_ready.connection_type = ConnectionType.network
    not_ready.ip_address = "192.168.1.53"
    manual = Device(
        pack_id="appium-roku-dlenroc",
        platform_id="roku_network",
        identity_scheme="roku_serial",
        identity_scope="global",
        identity_value="endpoint-manual",
        connection_target="192.168.1.54",
        name="Endpoint Manual",
        os_version="14",
        host_id=_host.id,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        ip_address="192.168.1.54",
        auto_manage=False,
    )
    failed_recovery = Device(
        pack_id="appium-roku-dlenroc",
        platform_id="roku_network",
        identity_scheme="roku_serial",
        identity_scope="global",
        identity_value="endpoint-failed-recovery",
        connection_target="192.168.1.55",
        name="Endpoint Failed Recovery",
        os_version="14",
        host_id=_host.id,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        ip_address="192.168.1.55",
    )
    db_session.add_all([manual, failed_recovery])
    await db_session.commit()

    _stub_settings(monkeypatch, threshold=2, timeout=2.0, count=1)
    monkeypatch.setattr("app.devices.services.connectivity._uses_endpoint_health", AsyncMock(return_value=True))
    monkeypatch.setattr("app.devices.services.connectivity._get_agent_devices", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        "app.devices.services.connectivity._get_device_health",
        AsyncMock(return_value={"healthy": True, "checks": [{"check_id": "ecp", "ok": True}]}),
    )

    async def endpoint_ready(_db: AsyncSession, device: Device) -> bool:
        return device.identity_value != "endpoint-not-ready"

    monkeypatch.setattr("app.devices.services.connectivity.is_ready_for_use_async", endpoint_ready)
    monkeypatch.setattr(
        "app.devices.services.connectivity.lifecycle_policy.attempt_auto_recovery",
        AsyncMock(return_value=False),
    )

    await _check_connectivity(db_session)

    assert "endpoint-not-ready" not in await get_connectivity_control_plane_state(db_session)
    assert "endpoint-manual" not in await get_connectivity_control_plane_state(db_session)
    assert "endpoint-failed-recovery" in await get_connectivity_control_plane_state(db_session)


async def test_running_avd_alias_keeps_stable_target_connected(db_session: AsyncSession) -> None:
    _host, device, node = await _setup_host_and_device(
        db_session,
        connection_target="Pixel_6_API_35",
        with_node=True,
    )
    device.identity_scheme = "manager_generated"
    device.identity_value = "avd:Pixel_6_API_35"
    device.device_type = "emulator"
    await db_session.commit()

    with (
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value={"emulator-5554", "Pixel_6_API_35", "avd:Pixel_6_API_35"},
        ),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
    ):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available
    assert node is not None
    await db_session.refresh(node)
    assert node.observed_running


async def test_running_avd_prefixed_alias_keeps_stable_target_connected(db_session: AsyncSession) -> None:
    _host, device, node = await _setup_host_and_device(
        db_session,
        connection_target="Pixel_6_API_35",
        with_node=True,
    )
    device.identity_scheme = "manager_generated"
    device.identity_value = "avd:Pixel_6_API_35"
    device.device_type = "emulator"
    await db_session.commit()

    with (
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value={"avd:Pixel_6_API_35"},
        ),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
    ):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available
    assert node is not None
    await db_session.refresh(node)
    assert node.observed_running


async def test_agent_device_aliases_include_running_avd_name(db_session: AsyncSession) -> None:
    host, _device, _node = await _setup_host_and_device(db_session)

    with patch(
        "app.devices.services.connectivity.get_pack_devices",
        new_callable=AsyncMock,
        return_value={
            "candidates": [
                {
                    "identity_value": "emulator-5554",
                    "detected_properties": {
                        "connection_target": "emulator-5554",
                        "avd_name": "Pixel_6_API_35",
                    },
                }
            ],
        },
    ):
        connected = await _get_agent_devices(host)

    assert connected == {"emulator-5554", "Pixel_6_API_35", "avd:Pixel_6_API_35"}


async def test_lifecycle_state_uses_pack_lifecycle_action(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.devices.services.connectivity._get_lifecycle_state", _get_lifecycle_state)
    _host, device, _node = await _setup_host_and_device(db_session)

    with patch(
        "app.devices.services.connectivity.pack_device_lifecycle_action",
        new_callable=AsyncMock,
        return_value={"state": "running"},
    ) as mock_lifecycle:
        state = await _get_lifecycle_state(db_session, device)

    assert state == "running"
    mock_lifecycle.assert_awaited_once()
    _, kwargs = mock_lifecycle.call_args
    assert kwargs["pack_id"] == "appium-uiautomator2"
    assert kwargs["platform_id"] == "android_mobile"
    assert kwargs["action"] == "state"


async def test_disconnected_device_marked_offline(db_session: AsyncSession) -> None:
    _host, device, node = await _setup_host_and_device(db_session, with_node=True)

    with patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline
    assert node is not None
    await db_session.refresh(node)
    assert node.observed_running
    assert node.desired_state == AppiumDesiredState.running
    assert node.accepting_new_sessions is False
    assert node.stop_pending is True


async def test_disconnected_device_writes_stop_intent(db_session: AsyncSession) -> None:
    _host, device, node = await _setup_host_and_device(db_session, with_node=True)

    with patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline
    assert node is not None
    await db_session.refresh(node)
    assert node.observed_running
    assert node.desired_state == AppiumDesiredState.running
    assert node.accepting_new_sessions is False
    assert node.stop_pending is True


async def test_offline_disconnected_device_stops_leftover_node(db_session: AsyncSession) -> None:
    _host, device, node = await _setup_host_and_device(
        db_session,
        device_operational_state=DeviceOperationalState.offline,
        with_node=True,
    )

    with patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline
    assert node is not None
    await db_session.refresh(node)
    assert node.observed_running
    assert node.desired_state == AppiumDesiredState.running
    assert node.accepting_new_sessions is False
    assert node.stop_pending is True


async def test_agent_unreachable_skips_host(db_session: AsyncSession) -> None:
    _host, device, _ = await _setup_host_and_device(db_session)

    with patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=None):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available  # unchanged


async def test_reappeared_device_auto_starts(db_session: AsyncSession) -> None:
    """When a device reappears after being offline, its node should auto-start."""
    _host, _device, _ = await _setup_host_and_device(
        db_session, device_operational_state=DeviceOperationalState.offline
    )
    # Mark as previously offline so it's recognized as a reappearance
    await track_previously_offline_device(db_session, "dc-001")

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
        patch(
            "app.devices.services.connectivity.lifecycle_policy.attempt_auto_recovery",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_recover,
    ):
        await _check_connectivity(db_session)

    mock_recover.assert_called_once()
    assert "dc-001" not in await get_connectivity_control_plane_state(db_session)


async def test_offline_device_auto_starts_on_startup_recovery(db_session: AsyncSession) -> None:
    """A connected offline device should auto-start on the first pass after manager startup."""
    _host, _device, _ = await _setup_host_and_device(
        db_session, device_operational_state=DeviceOperationalState.offline
    )

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
        patch(
            "app.devices.services.connectivity.lifecycle_policy.attempt_auto_recovery",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_recover,
    ):
        await _check_connectivity(db_session)

    mock_recover.assert_called_once()
    assert "dc-001" not in await get_connectivity_control_plane_state(db_session)


async def test_reappeared_device_auto_start_failure(db_session: AsyncSession) -> None:
    """If auto-start fails for a reappeared device, it stays offline."""
    _host, device, _ = await _setup_host_and_device(db_session, device_operational_state=DeviceOperationalState.offline)
    await track_previously_offline_device(db_session, "dc-001")

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
        patch(
            "app.devices.services.connectivity.lifecycle_policy.attempt_auto_recovery",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline  # still offline
    assert "dc-001" in await get_connectivity_control_plane_state(db_session)  # still tracked for next attempt


async def test_connected_offline_manual_device_skips_non_endpoint_auto_recovery(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host, device, _ = await _setup_host_and_device(
        db_session,
        device_operational_state=DeviceOperationalState.offline,
    )
    device.auto_manage = False
    await db_session.commit()
    monkeypatch.setattr("app.devices.services.connectivity.is_ready_for_use_async", AsyncMock(return_value=True))

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
    ):
        await _check_connectivity(db_session)

    assert "dc-001" not in await get_connectivity_control_plane_state(db_session)


async def test_offline_disconnected_device_tracks_stopped_leftover_node(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host, _device, _ = await _setup_host_and_device(
        db_session,
        device_operational_state=DeviceOperationalState.offline,
    )
    monkeypatch.setattr("app.devices.services.connectivity._stop_disconnected_node", AsyncMock(return_value=object()))

    with patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()):
        await _check_connectivity(db_session)

    assert "dc-001" in await get_connectivity_control_plane_state(db_session)


async def test_maintenance_device_not_touched(db_session: AsyncSession) -> None:
    """Maintenance devices should stay in maintenance when disconnected."""
    _host, device, _ = await _setup_host_and_device(db_session, device_hold=DeviceHold.maintenance)

    with patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.hold == DeviceHold.maintenance  # unchanged


async def test_connectivity_marks_busy_device_offline(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.devices.services import connectivity as device_connectivity
    from tests.helpers import create_device

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="busy-on-blip",
        operational_state=DeviceOperationalState.busy,
        auto_manage=True,
        verified=True,
    )
    await db_session.commit()

    async def fake_get_agent_devices(_host: Host) -> set[str]:
        return set()

    monkeypatch.setattr(device_connectivity, "_get_agent_devices", fake_get_agent_devices)

    await device_connectivity._check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline


async def test_connectivity_does_not_overwrite_reserved_with_offline(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.devices.services import connectivity as device_connectivity
    from tests.helpers import create_device

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="reserved-on-blip",
        hold=DeviceHold.reserved,
        auto_manage=True,
        verified=True,
    )
    await db_session.commit()

    async def fake_get_agent_devices(_host: Host) -> set[str]:
        return set()

    monkeypatch.setattr(device_connectivity, "_get_agent_devices", fake_get_agent_devices)

    await device_connectivity._check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.hold == DeviceHold.reserved


async def test_unhealthy_connected_device_triggers_policy_stop(db_session: AsyncSession) -> None:
    _host, device, _ = await _setup_host_and_device(
        db_session, device_operational_state=DeviceOperationalState.available
    )

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": False, "detail": "ADB not responsive"},
        ),
        patch(
            "app.devices.services.connectivity.lifecycle_policy.handle_health_failure",
            new_callable=AsyncMock,
        ) as mock_handle,
    ):
        await _check_connectivity(db_session)

    mock_handle.assert_called_once()
    await db_session.refresh(device)
    assert "dc-001" in await get_connectivity_control_plane_state(db_session)


async def test_connectivity_does_not_record_event_for_maintenance_blip(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disconnected maintenance device must not produce connectivity_lost or
    lifecycle_auto_stopped events; pre-PR behavior was silent and must be preserved."""
    from sqlalchemy import select

    from app.devices.models import DeviceEvent, DeviceEventType
    from app.devices.services import connectivity as device_connectivity
    from tests.helpers import create_device

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maintenance-blip",
        hold=DeviceHold.maintenance,
        auto_manage=True,
        verified=True,
    )
    await db_session.commit()

    async def fake_get_agent_devices(_host: Host) -> set[str]:
        return set()

    monkeypatch.setattr(device_connectivity, "_get_agent_devices", fake_get_agent_devices)

    await device_connectivity._check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.hold == DeviceHold.maintenance

    # No connectivity_lost event recorded
    events = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == device.id,
                    DeviceEvent.event_type == DeviceEventType.connectivity_lost,
                )
            )
        )
        .scalars()
        .all()
    )
    assert events == [], f"Maintenance device produced {len(events)} connectivity_lost event(s) — should be silent"

    # lifecycle_policy_state untouched (still default state)
    assert (device.lifecycle_policy_state or {}).get("last_failure_source") is None


# ---------------------------------------------------------------------------
# Task 12: integration test helpers
# ---------------------------------------------------------------------------


def healthy_payload(*, adb: bool = True, ip_ping: bool | None = None) -> dict[str, object]:
    checks: list[dict[str, object]] = [{"check_id": "adb", "ok": adb, "message": "" if adb else "adb dead"}]
    if ip_ping is not None:
        checks.append(
            {
                "check_id": "ip_ping",
                "ok": ip_ping,
                "message": "" if ip_ping else "ICMP echo unanswered",
            }
        )
    return {"healthy": adb and (ip_ping is None or ip_ping), "checks": checks}


def _stub_get_health(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    async def _f(device: object, **kwargs: object) -> object:
        return payload

    monkeypatch.setattr("app.devices.services.connectivity._get_device_health", _f)


def _stub_get_health_sequence(monkeypatch: pytest.MonkeyPatch, payloads: list[object]) -> None:
    iterator = iter(payloads)

    async def _f(device: object, **kwargs: object) -> object:
        return next(iterator)

    monkeypatch.setattr("app.devices.services.connectivity._get_device_health", _f)


def _stub_agent_devices(monkeypatch: pytest.MonkeyPatch, aliases: set[str]) -> None:
    async def _f(host: object) -> set[str]:
        return aliases

    monkeypatch.setattr("app.devices.services.connectivity._get_agent_devices", _f)


def _stub_settings(monkeypatch: pytest.MonkeyPatch, *, threshold: int, timeout: float, count: int) -> None:
    from app.settings import settings_service

    monkeypatch.setattr(
        settings_service,
        "get",
        _settings_dispatch(threshold=threshold, timeout=timeout, count=count),
    )


def _settings_dispatch(*, threshold: int, timeout: float, count: int) -> Callable[[str], object]:
    def _get(key: str) -> object:
        if key == "general.device_check_interval_sec":
            return 60
        if key == "device_checks.ip_ping.consecutive_fail_threshold":
            return threshold
        if key == "device_checks.ip_ping.timeout_sec":
            return timeout
        if key == "device_checks.ip_ping.count_per_cycle":
            return count
        raise KeyError(key)

    return _get


def _async_recorder(sink: list[str]) -> Callable[..., Coroutine[Any, Any, None]]:
    async def _f(*args: object, **kwargs: object) -> None:
        sink.append(str(kwargs.get("source", "unknown")))

    return _f


async def _reload(db: AsyncSession, device_id: object) -> Device:
    from sqlalchemy import select

    res = await db.execute(select(Device).where(Device.id == device_id))
    return res.scalar_one()


@pytest_asyncio.fixture
async def make_device(db_session: AsyncSession, db_host: Host) -> Callable[..., Coroutine[Any, Any, Device]]:
    """Factory fixture: ``await make_device(connection_type="usb", ip_address="...")``."""
    import uuid as _uuid

    from tests.helpers import create_device_record

    async def _factory(**kwargs: object) -> Device:
        identity = f"ip-dev-{_uuid.uuid4().hex[:8]}"
        return await create_device_record(
            db_session,
            host_id=db_host.id,
            identity_value=identity,
            name=f"Test Device {identity}",
            operational_state="available",
            auto_manage=kwargs.pop("auto_manage", True),  # type: ignore[arg-type]
            **kwargs,  # type: ignore[arg-type]
        )

    return _factory


# ---------------------------------------------------------------------------
# Task 11: ip_ping namespace constants and hysteresis helpers
# ---------------------------------------------------------------------------


class _FakeDevice:
    def __init__(self, identity_value: str) -> None:
        self.identity_value = identity_value


def test_split_ip_ping_separates_check() -> None:
    from app.devices.services.connectivity import _split_ip_ping

    checks = [
        {"check_id": "adb", "ok": True, "message": ""},
        {"check_id": "ip_ping", "ok": False, "message": "ICMP unanswered"},
    ]
    ip_ping, others = _split_ip_ping(checks)
    assert ip_ping == {"check_id": "ip_ping", "ok": False, "message": "ICMP unanswered"}
    assert others == [{"check_id": "adb", "ok": True, "message": ""}]


def test_split_ip_ping_when_absent() -> None:
    from app.devices.services.connectivity import _split_ip_ping

    checks = [{"check_id": "adb", "ok": True, "message": ""}]
    ip_ping, others = _split_ip_ping(checks)
    assert ip_ping is None
    assert others == checks


@pytest.mark.asyncio
async def test_apply_ip_ping_hysteresis_increments_below_threshold(db_session: AsyncSession) -> None:
    from app.devices.services.connectivity import IP_PING_NAMESPACE, _apply_ip_ping_hysteresis
    from app.services import control_plane_state_store

    fake = _FakeDevice(identity_value="dev-1")
    gated = await _apply_ip_ping_hysteresis(db_session, fake, ok=False, threshold=3)  # type: ignore[arg-type]
    assert gated is True
    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, "dev-1")
    assert counter == 1


@pytest.mark.asyncio
async def test_apply_ip_ping_hysteresis_flips_at_threshold(db_session: AsyncSession) -> None:
    from app.devices.services.connectivity import IP_PING_NAMESPACE, _apply_ip_ping_hysteresis
    from app.services import control_plane_state_store

    fake = _FakeDevice(identity_value="dev-1")
    for _ in range(2):
        await _apply_ip_ping_hysteresis(db_session, fake, ok=False, threshold=3)  # type: ignore[arg-type]
    gated = await _apply_ip_ping_hysteresis(db_session, fake, ok=False, threshold=3)  # type: ignore[arg-type]
    assert gated is False
    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, "dev-1")
    assert counter == 3


@pytest.mark.asyncio
async def test_apply_ip_ping_hysteresis_resets_on_success(db_session: AsyncSession) -> None:
    from app.devices.services.connectivity import IP_PING_NAMESPACE, _apply_ip_ping_hysteresis
    from app.services import control_plane_state_store

    fake = _FakeDevice(identity_value="dev-1")
    await _apply_ip_ping_hysteresis(db_session, fake, ok=False, threshold=3)  # type: ignore[arg-type]
    await _apply_ip_ping_hysteresis(db_session, fake, ok=False, threshold=3)  # type: ignore[arg-type]
    gated = await _apply_ip_ping_hysteresis(db_session, fake, ok=True, threshold=3)  # type: ignore[arg-type]
    assert gated is True
    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, "dev-1")
    assert counter is None


# ---------------------------------------------------------------------------
# Task 12: _check_connectivity ip_ping hysteresis integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ip_ping_first_miss_keeps_healthy(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    from app.devices.services.connectivity import IP_PING_NAMESPACE
    from app.services import control_plane_state_store

    device = await make_device(connection_type="usb", ip_address="10.0.0.7")
    _stub_settings(monkeypatch, threshold=3, timeout=2.0, count=1)
    _stub_get_health(monkeypatch, healthy_payload(adb=True, ip_ping=False))
    _stub_agent_devices(monkeypatch, {device.identity_value})

    await _check_connectivity(db_session)

    refreshed = await _reload(db_session, device.id)
    assert refreshed.device_checks_healthy is True
    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, device.identity_value)
    assert counter == 1


@pytest.mark.asyncio
async def test_ip_ping_threshold_flips_unhealthy(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    from app.devices.services.connectivity import IP_PING_NAMESPACE
    from app.services import control_plane_state_store

    device = await make_device(connection_type="usb", ip_address="10.0.0.7")
    _stub_settings(monkeypatch, threshold=3, timeout=2.0, count=1)
    _stub_get_health(monkeypatch, healthy_payload(adb=True, ip_ping=False))
    _stub_agent_devices(monkeypatch, {device.identity_value})
    handler_calls: list[str] = []
    monkeypatch.setattr(
        "app.devices.services.connectivity.lifecycle_policy.handle_health_failure",
        _async_recorder(handler_calls),
    )

    for _ in range(3):
        await _check_connectivity(db_session)

    refreshed = await _reload(db_session, device.id)
    assert refreshed.device_checks_healthy is False
    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, device.identity_value)
    assert counter == 3
    assert len(handler_calls) == 1


@pytest.mark.asyncio
async def test_ip_ping_success_clears_counter(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    from app.devices.services.connectivity import IP_PING_NAMESPACE
    from app.services import control_plane_state_store

    device = await make_device(connection_type="usb", ip_address="10.0.0.7")
    _stub_settings(monkeypatch, threshold=3, timeout=2.0, count=1)
    _stub_agent_devices(monkeypatch, {device.identity_value})
    payloads: list[object] = [
        healthy_payload(adb=True, ip_ping=False),
        healthy_payload(adb=True, ip_ping=False),
        healthy_payload(adb=True, ip_ping=True),
    ]
    _stub_get_health_sequence(monkeypatch, payloads)

    for _ in range(3):
        await _check_connectivity(db_session)

    refreshed = await _reload(db_session, device.id)
    assert refreshed.device_checks_healthy is True
    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, device.identity_value)
    assert counter is None


@pytest.mark.asyncio
async def test_ip_ping_other_check_failure_no_hysteresis(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    from app.devices.services.connectivity import IP_PING_NAMESPACE
    from app.services import control_plane_state_store

    device = await make_device(connection_type="usb", ip_address="10.0.0.7")
    _stub_settings(monkeypatch, threshold=3, timeout=2.0, count=1)
    _stub_get_health(monkeypatch, healthy_payload(adb=False, ip_ping=True))
    _stub_agent_devices(monkeypatch, {device.identity_value})

    await _check_connectivity(db_session)

    refreshed = await _reload(db_session, device.id)
    assert refreshed.device_checks_healthy is False
    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, device.identity_value)
    assert counter is None


@pytest.mark.asyncio
async def test_ip_ping_absent_no_counter_writes(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    from app.devices.services.connectivity import IP_PING_NAMESPACE
    from app.services import control_plane_state_store

    device = await make_device(connection_type="usb", ip_address=None)
    _stub_settings(monkeypatch, threshold=3, timeout=2.0, count=1)
    _stub_get_health(monkeypatch, healthy_payload(adb=True))  # no ip_ping entry
    _stub_agent_devices(monkeypatch, {device.identity_value})

    await _check_connectivity(db_session)

    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, device.identity_value)
    assert counter is None


@pytest.mark.asyncio
async def test_ip_ping_skipped_for_held_device(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    from app.devices.services.connectivity import IP_PING_NAMESPACE
    from app.services import control_plane_state_store

    device = await make_device(connection_type="usb", ip_address="10.0.0.7", hold=DeviceHold.maintenance)
    _stub_settings(monkeypatch, threshold=3, timeout=2.0, count=1)
    _stub_get_health(monkeypatch, healthy_payload(adb=True, ip_ping=False))
    _stub_agent_devices(monkeypatch, {device.identity_value})

    await _check_connectivity(db_session)

    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, device.identity_value)
    assert counter is None


@pytest.mark.asyncio
async def test_ip_ping_skipped_for_auto_manage_off(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    from app.devices.services.connectivity import IP_PING_NAMESPACE
    from app.services import control_plane_state_store

    device = await make_device(connection_type="usb", ip_address="10.0.0.7", auto_manage=False)
    _stub_settings(monkeypatch, threshold=3, timeout=2.0, count=1)
    _stub_get_health(monkeypatch, healthy_payload(adb=True, ip_ping=False))
    _stub_agent_devices(monkeypatch, {device.identity_value})

    await _check_connectivity(db_session)

    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, device.identity_value)
    assert counter is None


@pytest.mark.asyncio
async def test_ip_ping_health_result_none_preserves_counter(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    from app.devices.services.connectivity import IP_PING_NAMESPACE
    from app.services import control_plane_state_store

    device = await make_device(connection_type="usb", ip_address="10.0.0.7")
    _stub_settings(monkeypatch, threshold=3, timeout=2.0, count=1)
    _stub_agent_devices(monkeypatch, {device.identity_value})
    await control_plane_state_store.set_value(db_session, IP_PING_NAMESPACE, device.identity_value, 2)
    await db_session.commit()
    _stub_get_health(monkeypatch, None)  # agent unreachable

    await _check_connectivity(db_session)

    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, device.identity_value)
    assert counter == 2


@pytest.mark.asyncio
async def test_ip_ping_settings_threshold_one_flips_immediately(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    device = await make_device(connection_type="usb", ip_address="10.0.0.7")
    _stub_settings(monkeypatch, threshold=1, timeout=2.0, count=1)
    _stub_get_health(monkeypatch, healthy_payload(adb=True, ip_ping=False))
    _stub_agent_devices(monkeypatch, {device.identity_value})

    await _check_connectivity(db_session)

    refreshed = await _reload(db_session, device.id)
    assert refreshed.device_checks_healthy is False


# ---------------------------------------------------------------------------
# Task 13: delete_device clears control_plane_state namespace rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_device_clears_connectivity_and_ip_ping_namespaces(
    db_session: AsyncSession,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    from app.devices.services.connectivity import CONNECTIVITY_NAMESPACE, IP_PING_NAMESPACE
    from app.devices.services.service import delete_device
    from app.services import control_plane_state_store

    device = await make_device(connection_type="usb", ip_address="10.0.0.7")
    await control_plane_state_store.set_value(db_session, IP_PING_NAMESPACE, device.identity_value, 2)
    await control_plane_state_store.set_value(db_session, CONNECTIVITY_NAMESPACE, device.identity_value, True)
    await db_session.commit()

    deleted = await delete_device(db_session, device.id)
    assert deleted is True

    ip_ping_counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, device.identity_value)
    assert ip_ping_counter is None

    connectivity_flag = await control_plane_state_store.get_value(
        db_session, CONNECTIVITY_NAMESPACE, device.identity_value
    )
    assert connectivity_flag is None
