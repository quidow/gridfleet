from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import ConnectionType, Device, DeviceHold, DeviceOperationalState, DeviceType
from app.models.host import Host, HostStatus
from app.services.device_connectivity import (
    _check_connectivity,
    _get_agent_devices,
    _get_lifecycle_state,
    get_connectivity_control_plane_state,
    track_previously_offline_device,
)

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


@pytest.fixture(autouse=True)
def _skip_lifecycle_state_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.device_connectivity._get_lifecycle_state", AsyncMock(return_value=None))


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
        node = AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444", state=NodeState.running)
        db_session.add(node)

    await db_session.commit()
    return host, device, node


async def test_connected_device_stays_available(db_session: AsyncSession) -> None:
    _host, device, _ = await _setup_host_and_device(db_session)

    with (
        patch("app.services.device_connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.services.device_connectivity._get_device_health",
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
        patch("app.services.device_connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch(
            "app.services.device_connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True, "checks": [{"check_id": "ecp", "ok": True}]},
        ) as health,
        patch("app.services.device_connectivity._stop_node_via_agent", new_callable=AsyncMock) as mock_stop,
    ):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available
    health.assert_awaited_once()
    mock_stop.assert_not_called()
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
        patch("app.services.device_connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch(
            "app.services.device_connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True, "checks": [{"check_id": "ecp", "ok": True}]},
        ),
        patch(
            "app.services.device_connectivity.lifecycle_policy.attempt_auto_recovery",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_recover,
    ):
        await _check_connectivity(db_session)

    mock_recover.assert_called_once()
    _, kwargs = mock_recover.call_args
    assert kwargs["reason"] == "Startup recovery after healthy endpoint check"
    assert "YJ1234567890" not in await get_connectivity_control_plane_state(db_session)


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
            "app.services.device_connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value={"emulator-5554", "Pixel_6_API_35", "avd:Pixel_6_API_35"},
        ),
        patch(
            "app.services.device_connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
        patch("app.services.device_connectivity._stop_node_via_agent", new_callable=AsyncMock) as mock_stop,
    ):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available
    mock_stop.assert_not_called()
    assert node is not None
    await db_session.refresh(node)
    assert node.state == NodeState.running


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
            "app.services.device_connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value={"avd:Pixel_6_API_35"},
        ),
        patch(
            "app.services.device_connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
        patch("app.services.device_connectivity._stop_node_via_agent", new_callable=AsyncMock) as mock_stop,
    ):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available
    mock_stop.assert_not_called()
    assert node is not None
    await db_session.refresh(node)
    assert node.state == NodeState.running


async def test_agent_device_aliases_include_running_avd_name(db_session: AsyncSession) -> None:
    host, _device, _node = await _setup_host_and_device(db_session)

    with patch(
        "app.services.device_connectivity.get_pack_devices",
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
    monkeypatch.setattr("app.services.device_connectivity._get_lifecycle_state", _get_lifecycle_state)
    _host, device, _node = await _setup_host_and_device(db_session)

    with patch(
        "app.services.device_connectivity.pack_device_lifecycle_action",
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

    # Agent reports no devices connected
    with (
        patch("app.services.device_connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch(
            "app.services.device_connectivity._stop_node_via_agent", new_callable=AsyncMock, return_value=True
        ) as mock_stop,
    ):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline
    mock_stop.assert_called_once()
    assert node is not None
    await db_session.refresh(node)
    assert node.state == NodeState.stopped


async def test_disconnected_device_keeps_node_error_when_stop_fails(db_session: AsyncSession) -> None:
    _host, device, node = await _setup_host_and_device(db_session, with_node=True)

    with (
        patch("app.services.device_connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch(
            "app.services.device_connectivity._stop_node_via_agent",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_stop,
    ):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline
    mock_stop.assert_awaited_once()
    assert node is not None
    await db_session.refresh(node)
    assert node.state == NodeState.error


async def test_offline_disconnected_device_stops_leftover_node(db_session: AsyncSession) -> None:
    _host, device, node = await _setup_host_and_device(
        db_session,
        device_operational_state=DeviceOperationalState.offline,
        with_node=True,
    )

    with (
        patch("app.services.device_connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch(
            "app.services.device_connectivity._stop_node_via_agent", new_callable=AsyncMock, return_value=True
        ) as stop,
        patch("app.services.device_connectivity.record_event", new_callable=AsyncMock) as record,
    ):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline
    stop.assert_awaited_once()
    record.assert_not_awaited()
    assert node is not None
    await db_session.refresh(node)
    assert node.state == NodeState.stopped


async def test_agent_unreachable_skips_host(db_session: AsyncSession) -> None:
    _host, device, _ = await _setup_host_and_device(db_session)

    with patch("app.services.device_connectivity._get_agent_devices", new_callable=AsyncMock, return_value=None):
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
        patch("app.services.device_connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.services.device_connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
        patch(
            "app.services.device_connectivity.lifecycle_policy.attempt_auto_recovery",
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
        patch("app.services.device_connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.services.device_connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
        patch(
            "app.services.device_connectivity.lifecycle_policy.attempt_auto_recovery",
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
        patch("app.services.device_connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.services.device_connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
        patch(
            "app.services.device_connectivity.lifecycle_policy.attempt_auto_recovery",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline  # still offline
    assert "dc-001" in await get_connectivity_control_plane_state(db_session)  # still tracked for next attempt


async def test_maintenance_device_not_touched(db_session: AsyncSession) -> None:
    """Maintenance devices should stay in maintenance when disconnected."""
    _host, device, _ = await _setup_host_and_device(db_session, device_hold=DeviceHold.maintenance)

    with patch("app.services.device_connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()):
        await _check_connectivity(db_session)

    await db_session.refresh(device)
    assert device.hold == DeviceHold.maintenance  # unchanged


async def test_connectivity_marks_busy_device_offline(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import device_connectivity
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
    from app.services import device_connectivity
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
        patch("app.services.device_connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.services.device_connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": False, "detail": "ADB not responsive"},
        ),
        patch(
            "app.services.device_connectivity.lifecycle_policy.handle_health_failure",
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

    from app.models.device_event import DeviceEvent, DeviceEventType
    from app.services import device_connectivity
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
