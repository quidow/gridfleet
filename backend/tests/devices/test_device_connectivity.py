from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import pytest_asyncio

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core.errors import AgentCallError
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services import connectivity as device_connectivity
from app.devices.services.connectivity import (
    ConnectivityService,
    _fetch_lifecycle_state,
    _get_agent_devices,
    _get_device_health,
    _lifecycle_state_capable,
)
from app.devices.services.health import DeviceHealthService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.hosts.models import Host, HostStatus
from tests.fakes import FakeSettingsReader
from tests.helpers import get_connectivity_control_plane_state, track_previously_offline_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable, Coroutine

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


@pytest.fixture(autouse=True)
def _skip_lifecycle_state_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.devices.services.connectivity._lifecycle_state_capable", AsyncMock(return_value=False))
    monkeypatch.setattr("app.devices.services.connectivity._fetch_lifecycle_state", AsyncMock(return_value=None))


async def _run_connectivity_fold(service: ConnectivityService, db: AsyncSession) -> None:
    """Fold the pushed device_health section once per online host — the fan-out
    the deleted check_connectivity pass used to do. Each device's observation is
    sourced from the (mocked) _get_device_health so the legacy per-device mocks
    keep working; the fold's internal post-repair re-probe still dials the same
    mock, consuming the next side_effect element."""
    from sqlalchemy import select as _select

    from app.core.timeutil import now_utc as _now_utc

    hosts = (await db.execute(_select(Host).where(Host.status == HostStatus.online))).scalars().all()
    for _host in hosts:
        _devices = (await db.execute(_select(Device).where(Device.host_id == _host.id))).scalars().all()
        _observations: dict[str, object] = {}
        for _d in _devices:
            _payload = await device_connectivity._get_device_health(_d)
            if _payload is not None and _d.connection_target:
                _observations[_d.connection_target] = _payload
        await service.fold_host_device_health(
            db, _host.id, {"reported_at": _now_utc().isoformat(), "devices": _observations}
        )


async def _setup_host_and_device(
    db_session: AsyncSession,
    connection_target: str = "dc-001",
    device_operational_state: DeviceOperationalState = DeviceOperationalState.available,
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
        lifecycle_policy_state=(
            {"maintenance_reason": "test maintenance"}
            if device_operational_state == DeviceOperationalState.maintenance
            else {}
        ),
        session_viability_status=("failed" if device_operational_state == DeviceOperationalState.offline else None),
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
    assert await _get_agent_devices(host, settings=FakeSettingsReader({}), circuit_breaker=Mock()) == set()

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
    aliases = await _get_agent_devices(host, settings=FakeSettingsReader({}), circuit_breaker=Mock())
    assert aliases is not None
    assert {"serial-1", "serial-2", "10.0.0.5:5555"} <= aliases

    monkeypatch.setattr(
        "app.devices.services.connectivity.get_pack_devices",
        AsyncMock(side_effect=AgentCallError("10.0.0.10", "down")),
    )
    assert await _get_agent_devices(host, settings=FakeSettingsReader({}), circuit_breaker=Mock()) is None


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
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available


async def test_healthy_available_device_triggers_self_heal_clear(db_session: AsyncSession) -> None:
    """A healthy, non-offline device routes through the self-heal clearer so any
    stale recovery-suppression residue left by a prior failed recovery is cleared."""
    _host, _device, _ = await _setup_host_and_device(db_session)
    lifecycle_policy = AsyncMock()

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=lifecycle_policy,
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    lifecycle_policy.clear_escalation_residue_on_self_heal.assert_awaited_once()


async def test_healthy_available_device_attempts_run_restore(db_session: AsyncSession) -> None:
    """A healthy, non-offline device also routes through the run-restore path so a stale
    health-failure run exclusion (left by a recovery route that skipped restore) clears."""
    _host, _device, _ = await _setup_host_and_device(db_session)
    lifecycle_policy = AsyncMock()

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=lifecycle_policy,
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    lifecycle_policy.restore_run_after_self_heal.assert_awaited_once()


async def test_disconnected_device_skips_self_heal_clear(db_session: AsyncSession) -> None:
    """An unhealthy/disconnected device never reaches the healthy self-heal path,
    so its suppression residue is left untouched."""
    _host, _device, _node = await _setup_host_and_device(db_session, with_node=True)
    lifecycle_policy = AsyncMock()

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch("app.devices.services.connectivity._get_device_health", new_callable=AsyncMock, return_value=None),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=lifecycle_policy,
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    lifecycle_policy.clear_escalation_residue_on_self_heal.assert_not_awaited()


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
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

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

    mock_recover = AsyncMock(return_value=True)
    mock_lifecycle_policy = MagicMock()
    mock_lifecycle_policy.attempt_auto_recovery = mock_recover

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True, "checks": [{"check_id": "ecp", "ok": True}]},
        ),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=mock_lifecycle_policy,
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    mock_recover.assert_called_once()
    _, kwargs = mock_recover.call_args
    assert kwargs["reason"] == "Startup recovery after healthy reconnect"
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

    settings = _stub_settings(monkeypatch, threshold=2, timeout=2.0, count=1)
    monkeypatch.setattr("app.devices.services.connectivity._get_agent_devices", AsyncMock(return_value=set()))

    async def endpoint_health(device: Device, **_kwargs: object) -> dict[str, object]:
        if device.identity_value == "endpoint-failing":
            return {"healthy": False}
        return healthy_payload(adb=True, ip_ping=False)

    monkeypatch.setattr("app.devices.services.connectivity._get_device_health", endpoint_health)

    mock_lifecycle_policy = AsyncMock()
    await _run_connectivity_fold(
        ConnectivityService(
            publisher=Mock(),
            settings=settings,
            circuit_breaker=Mock(),
            lifecycle_policy=mock_lifecycle_policy,
            health=DeviceHealthService(publisher=Mock()),
        ),
        db_session,
    )

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
    monkeypatch.setattr("app.devices.services.connectivity._get_agent_devices", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        "app.devices.services.connectivity._get_device_health",
        AsyncMock(return_value={"healthy": True, "checks": [{"check_id": "ecp", "ok": True}]}),
    )

    async def endpoint_ready(_db: AsyncSession, device: Device) -> bool:
        return device.identity_value != "endpoint-not-ready"

    monkeypatch.setattr("app.devices.services.connectivity.is_ready_for_use_async", endpoint_ready)

    mock_lifecycle_policy = MagicMock()
    mock_lifecycle_policy.attempt_auto_recovery = AsyncMock(return_value=False)
    await _run_connectivity_fold(
        ConnectivityService(
            publisher=Mock(),
            settings=FakeSettingsReader({}),
            circuit_breaker=Mock(),
            lifecycle_policy=mock_lifecycle_policy,
            health=DeviceHealthService(publisher=Mock()),
        ),
        db_session,
    )

    assert "endpoint-not-ready" not in await get_connectivity_control_plane_state(db_session)
    assert "endpoint-manual" in await get_connectivity_control_plane_state(db_session)
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
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

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
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

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
        connected = await _get_agent_devices(host, settings=FakeSettingsReader({}), circuit_breaker=Mock())

    assert connected == {"emulator-5554", "Pixel_6_API_35", "avd:Pixel_6_API_35"}


async def test_lifecycle_state_uses_pack_lifecycle_action(
    db_session: AsyncSession,
) -> None:
    _host, device, _node = await _setup_host_and_device(db_session)

    assert await _lifecycle_state_capable(db_session, device) is True
    with patch(
        "app.devices.services.connectivity.pack_device_lifecycle_action",
        new_callable=AsyncMock,
        return_value={"state": "running"},
    ) as mock_lifecycle:
        state = await _fetch_lifecycle_state(device, settings=FakeSettingsReader({}), circuit_breaker=Mock())

    assert state == "running"
    mock_lifecycle.assert_awaited_once()
    _, kwargs = mock_lifecycle.call_args
    assert kwargs["pack_id"] == "appium-uiautomator2"
    assert kwargs["platform_id"] == "android_mobile"
    assert kwargs["action"] == "state"


async def test_disconnected_device_marked_offline(db_session: AsyncSession) -> None:
    _host, device, node = await _setup_host_and_device(db_session, with_node=True)

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch("app.devices.services.connectivity._get_device_health", new_callable=AsyncMock, return_value=None),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

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

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch("app.devices.services.connectivity._get_device_health", new_callable=AsyncMock, return_value=None),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

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

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch("app.devices.services.connectivity._get_device_health", new_callable=AsyncMock, return_value=None),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

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

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=None),
        patch("app.devices.services.connectivity._get_device_health", new_callable=AsyncMock, return_value=None),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available  # unchanged


async def test_reappeared_device_auto_starts(db_session: AsyncSession) -> None:
    """When a device reappears after being offline, its node should auto-start."""
    _host, _device, _ = await _setup_host_and_device(
        db_session, device_operational_state=DeviceOperationalState.offline
    )
    # Mark as previously offline so it's recognized as a reappearance
    await track_previously_offline_device(db_session, "dc-001")

    mock_recover = AsyncMock(return_value=True)
    mock_lifecycle_policy = MagicMock()
    mock_lifecycle_policy.attempt_auto_recovery = mock_recover

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=mock_lifecycle_policy,
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    mock_recover.assert_called_once()
    assert "dc-001" not in await get_connectivity_control_plane_state(db_session)


async def test_offline_device_auto_starts_on_startup_recovery(db_session: AsyncSession) -> None:
    """A connected offline device should auto-start on the first pass after manager startup."""
    _host, _device, _ = await _setup_host_and_device(
        db_session, device_operational_state=DeviceOperationalState.offline
    )

    mock_recover = AsyncMock(return_value=True)
    mock_lifecycle_policy = MagicMock()
    mock_lifecycle_policy.attempt_auto_recovery = mock_recover

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=mock_lifecycle_policy,
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    mock_recover.assert_called_once()
    assert "dc-001" not in await get_connectivity_control_plane_state(db_session)


async def test_reappeared_device_auto_start_failure(db_session: AsyncSession) -> None:
    """If auto-start fails for a reappeared device, it stays offline."""
    _host, device, _ = await _setup_host_and_device(db_session, device_operational_state=DeviceOperationalState.offline)
    await track_previously_offline_device(db_session, "dc-001")

    mock_lifecycle_policy = MagicMock()
    mock_lifecycle_policy.attempt_auto_recovery = AsyncMock(return_value=False)

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=mock_lifecycle_policy,
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline  # still offline
    assert "dc-001" in await get_connectivity_control_plane_state(db_session)  # still tracked for next attempt


async def test_maintenance_device_not_touched(db_session: AsyncSession) -> None:
    """Maintenance devices should stay in maintenance when disconnected."""
    _host, device, _ = await _setup_host_and_device(
        db_session, device_operational_state=DeviceOperationalState.maintenance
    )

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch("app.devices.services.connectivity._get_device_health", new_callable=AsyncMock, return_value=None),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.maintenance  # unchanged


async def test_connectivity_maintenance_disconnect_skipped_silently(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disconnected maintenance device (operational_state=maintenance, hold NULL) is
    skipped before any lifecycle write — gating reads operational_state, not hold."""
    from app.devices.services import connectivity as device_connectivity
    from tests.helpers import create_device

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maintenance-skip",
        operational_state=DeviceOperationalState.maintenance,
        lifecycle_policy_state={"maintenance_reason": "test maintenance"},
        verified=True,
    )
    await db_session.commit()

    async def fake_get_agent_devices(
        _host: Host, *, settings: object, circuit_breaker: object, pool: object = None
    ) -> set[str]:
        del settings, circuit_breaker, pool
        return set()

    monkeypatch.setattr(device_connectivity, "_get_agent_devices", fake_get_agent_devices)
    monkeypatch.setattr(device_connectivity, "_get_device_health", AsyncMock(return_value=None))

    mock_lifecycle_policy = AsyncMock()
    await _run_connectivity_fold(
        ConnectivityService(
            publisher=Mock(),
            settings=FakeSettingsReader({}),
            circuit_breaker=Mock(),
            lifecycle_policy=mock_lifecycle_policy,
            health=DeviceHealthService(publisher=Mock()),
        ),
        db_session,
    )

    mock_lifecycle_policy.note_connectivity_loss.assert_not_awaited()
    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.maintenance


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
        verified=True,
    )
    await db_session.commit()

    async def fake_get_agent_devices(
        _host: Host, *, settings: object, circuit_breaker: object, pool: object = None
    ) -> set[str]:
        del settings, circuit_breaker, pool
        return set()

    monkeypatch.setattr(device_connectivity, "_get_agent_devices", fake_get_agent_devices)
    monkeypatch.setattr(device_connectivity, "_get_device_health", AsyncMock(return_value=None))

    await _run_connectivity_fold(
        ConnectivityService(
            publisher=Mock(),
            settings=FakeSettingsReader({}),
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=DeviceHealthService(publisher=Mock()),
        ),
        db_session,
    )

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline


async def test_connectivity_reserved_device_takes_warning_path_not_idle(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device with an active reservation row (hold NULL) that disconnects must take the
    busy/reserved warning path — marking checks unhealthy and routing through the lifecycle
    note — not the idle offline-reconcile path."""
    from app.devices.services import connectivity as device_connectivity
    from tests.helpers import create_device, create_reservation

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="reserved-on-blip",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    await create_reservation(db_session, device_id=device.id)
    await db_session.commit()

    async def fake_get_agent_devices(
        _host: Host, *, settings: object, circuit_breaker: object, pool: object = None
    ) -> set[str]:
        del settings, circuit_breaker, pool
        return set()

    monkeypatch.setattr(device_connectivity, "_get_agent_devices", fake_get_agent_devices)
    monkeypatch.setattr(device_connectivity, "_get_device_health", AsyncMock(return_value=None))

    mock_lifecycle_policy = AsyncMock()
    await _run_connectivity_fold(
        ConnectivityService(
            publisher=Mock(),
            settings=FakeSettingsReader({}),
            circuit_breaker=Mock(),
            lifecycle_policy=mock_lifecycle_policy,
            health=DeviceHealthService(publisher=Mock()),
        ),
        db_session,
    )

    # Reserved device routes through note_connectivity_loss (warning path), not the idle path.
    mock_lifecycle_policy.note_connectivity_loss.assert_awaited_once()


async def test_unhealthy_connected_device_triggers_policy_stop(db_session: AsyncSession) -> None:
    _host, device, _ = await _setup_host_and_device(
        db_session, device_operational_state=DeviceOperationalState.available
    )

    mock_handle = AsyncMock()
    mock_lifecycle_policy = MagicMock()
    mock_lifecycle_policy.handle_health_failure = mock_handle

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": False, "detail": "ADB not responsive"},
        ),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=mock_lifecycle_policy,
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

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
        operational_state=DeviceOperationalState.maintenance,
        lifecycle_policy_state={"maintenance_reason": "test maintenance"},
        verified=True,
    )
    await db_session.commit()

    async def fake_get_agent_devices(
        _host: Host, *, settings: object, circuit_breaker: object, pool: object = None
    ) -> set[str]:
        del settings, circuit_breaker, pool
        return set()

    monkeypatch.setattr(device_connectivity, "_get_agent_devices", fake_get_agent_devices)
    monkeypatch.setattr(device_connectivity, "_get_device_health", AsyncMock(return_value=None))

    await _run_connectivity_fold(
        ConnectivityService(
            publisher=Mock(),
            settings=FakeSettingsReader({}),
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=DeviceHealthService(publisher=Mock()),
        ),
        db_session,
    )

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.maintenance

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


async def test_disconnect_records_exactly_one_connectivity_lost_event(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PIN (plan 4b): one disconnect episode == exactly one connectivity_lost audit row.

    Analytics reliability counts query this type; the row must survive the move from the
    transition path to the observation site with identical cardinality. Fix the
    implementation, never this pin.
    """
    from sqlalchemy import select

    from app.devices.models import DeviceEvent, DeviceEventType
    from app.devices.services import connectivity as device_connectivity
    from tests.helpers import create_device

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="disconnect-pin",
        operational_state=DeviceOperationalState.available,
        verified=True,
        device_checks_healthy=True,
    )
    await db_session.commit()

    async def fake_get_agent_devices(
        _host: Host, *, settings: object, circuit_breaker: object, pool: object = None
    ) -> set[str]:
        del settings, circuit_breaker, pool
        return set()

    monkeypatch.setattr(device_connectivity, "_get_agent_devices", fake_get_agent_devices)
    monkeypatch.setattr(device_connectivity, "_get_device_health", AsyncMock(return_value=None))

    async def fake_update_device_checks(
        _db_session: AsyncSession, target: Device, *, healthy: bool, summary: str
    ) -> None:
        target.device_checks_healthy = healthy
        target.device_checks_summary = summary

    service = ConnectivityService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        lifecycle_policy=AsyncMock(),
        health=SimpleNamespace(update_device_checks=fake_update_device_checks),
    )
    await _run_connectivity_fold(service, db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline

    async def connectivity_lost_count() -> int:
        rows = (
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
        return len(rows)

    assert await connectivity_lost_count() == 1

    # A second pass while the device stays disconnected must not add a row.
    await _run_connectivity_fold(service, db_session)
    assert await connectivity_lost_count() == 1


async def test_disconnect_of_busy_device_records_connectivity_lost(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disconnect detected while a session keeps the device busy records connectivity_lost
    at detection time. Previously the busy mask suppressed the axis flip and no row was ever
    written for these episodes (plan 4b behavior change #3)."""
    from sqlalchemy import select

    from app.devices.models import DeviceEvent, DeviceEventType
    from app.devices.services import connectivity as device_connectivity
    from app.sessions.models import Session, SessionStatus
    from tests.helpers import create_device

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="disconnect-busy",
        operational_state=DeviceOperationalState.busy,
        verified=True,
        device_checks_healthy=True,
    )
    db_session.add(Session(session_id="disc-busy-1", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()

    async def fake_get_agent_devices(
        _host: Host, *, settings: object, circuit_breaker: object, pool: object = None
    ) -> set[str]:
        del settings, circuit_breaker, pool
        return set()

    monkeypatch.setattr(device_connectivity, "_get_agent_devices", fake_get_agent_devices)
    monkeypatch.setattr(device_connectivity, "_get_device_health", AsyncMock(return_value=None))

    await _run_connectivity_fold(
        ConnectivityService(
            publisher=Mock(),
            settings=FakeSettingsReader({}),
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=DeviceHealthService(publisher=Mock()),
        ),
        db_session,
    )

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.busy  # session mask holds

    rows = (
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
    assert len(rows) == 1


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
    async def _f(host: object, *, settings: object, circuit_breaker: object, pool: object = None) -> set[str]:
        del settings, circuit_breaker, pool
        return aliases

    monkeypatch.setattr("app.devices.services.connectivity._get_agent_devices", _f)


def _stub_settings(
    monkeypatch: pytest.MonkeyPatch, *, threshold: int, timeout: float, count: int
) -> FakeSettingsReader:
    from tests.conftest import settings_service

    dispatcher = _settings_dispatch(threshold=threshold, timeout=timeout, count=count)
    monkeypatch.setattr(settings_service, "get", dispatcher)
    return FakeSettingsReader(
        {
            "device_checks.ip_ping.consecutive_fail_threshold": dispatcher(
                "device_checks.ip_ping.consecutive_fail_threshold"
            ),
            "device_checks.ip_ping.timeout_sec": dispatcher("device_checks.ip_ping.timeout_sec"),
            "device_checks.ip_ping.count_per_cycle": dispatcher("device_checks.ip_ping.count_per_cycle"),
        }
    )


def _settings_dispatch(*, threshold: int, timeout: float, count: int) -> Callable[[str], object]:
    def _get(key: str) -> object:
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
        kwargs.setdefault("operational_state", "available")
        return await create_device_record(
            db_session,
            host_id=db_host.id,
            identity_value=identity,
            name=f"Test Device {identity}",
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
    from app.core.leader import state_store as control_plane_state_store
    from app.devices.services.connectivity import IP_PING_NAMESPACE, _apply_failure_hysteresis

    fake = _FakeDevice(identity_value="dev-1")
    gated = await _apply_failure_hysteresis(db_session, fake, namespace=IP_PING_NAMESPACE, ok=False, threshold=3)  # type: ignore[arg-type]
    assert gated is True
    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, "dev-1")
    assert counter == 1


@pytest.mark.asyncio
async def test_apply_ip_ping_hysteresis_flips_at_threshold(db_session: AsyncSession) -> None:
    from app.core.leader import state_store as control_plane_state_store
    from app.devices.services.connectivity import IP_PING_NAMESPACE, _apply_failure_hysteresis

    fake = _FakeDevice(identity_value="dev-1")
    for _ in range(2):
        await _apply_failure_hysteresis(db_session, fake, namespace=IP_PING_NAMESPACE, ok=False, threshold=3)  # type: ignore[arg-type]
    gated = await _apply_failure_hysteresis(db_session, fake, namespace=IP_PING_NAMESPACE, ok=False, threshold=3)  # type: ignore[arg-type]
    assert gated is False
    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, "dev-1")
    assert counter == 3


@pytest.mark.asyncio
async def test_apply_ip_ping_hysteresis_resets_on_success(db_session: AsyncSession) -> None:
    from app.core.leader import state_store as control_plane_state_store
    from app.devices.services.connectivity import IP_PING_NAMESPACE, _apply_failure_hysteresis

    fake = _FakeDevice(identity_value="dev-1")
    await _apply_failure_hysteresis(db_session, fake, namespace=IP_PING_NAMESPACE, ok=False, threshold=3)  # type: ignore[arg-type]
    await _apply_failure_hysteresis(db_session, fake, namespace=IP_PING_NAMESPACE, ok=False, threshold=3)  # type: ignore[arg-type]
    gated = await _apply_failure_hysteresis(db_session, fake, namespace=IP_PING_NAMESPACE, ok=True, threshold=3)  # type: ignore[arg-type]
    assert gated is True
    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, "dev-1")
    assert counter is None


@pytest.mark.asyncio
async def test_apply_probe_failure_hysteresis_increments_below_threshold(db_session: AsyncSession) -> None:
    from app.core.leader import state_store as control_plane_state_store
    from app.devices.services.connectivity import PROBE_FAILED_NAMESPACE, _apply_failure_hysteresis

    fake = _FakeDevice(identity_value="dev-1")
    gated = await _apply_failure_hysteresis(db_session, fake, namespace=PROBE_FAILED_NAMESPACE, ok=False, threshold=3)  # type: ignore[arg-type]
    assert gated is True
    counter = await control_plane_state_store.get_value(db_session, PROBE_FAILED_NAMESPACE, "dev-1")
    assert counter == 1


@pytest.mark.asyncio
async def test_apply_probe_failure_hysteresis_flips_at_threshold(db_session: AsyncSession) -> None:
    from app.core.leader import state_store as control_plane_state_store
    from app.devices.services.connectivity import PROBE_FAILED_NAMESPACE, _apply_failure_hysteresis

    fake = _FakeDevice(identity_value="dev-1")
    for _ in range(2):
        await _apply_failure_hysteresis(db_session, fake, namespace=PROBE_FAILED_NAMESPACE, ok=False, threshold=3)  # type: ignore[arg-type]
    gated = await _apply_failure_hysteresis(db_session, fake, namespace=PROBE_FAILED_NAMESPACE, ok=False, threshold=3)  # type: ignore[arg-type]
    assert gated is False
    counter = await control_plane_state_store.get_value(db_session, PROBE_FAILED_NAMESPACE, "dev-1")
    assert counter == 3


@pytest.mark.asyncio
async def test_apply_probe_failure_hysteresis_resets_on_success(db_session: AsyncSession) -> None:
    from app.core.leader import state_store as control_plane_state_store
    from app.devices.services.connectivity import PROBE_FAILED_NAMESPACE, _apply_failure_hysteresis

    fake = _FakeDevice(identity_value="dev-1")
    await _apply_failure_hysteresis(db_session, fake, namespace=PROBE_FAILED_NAMESPACE, ok=False, threshold=3)  # type: ignore[arg-type]
    await _apply_failure_hysteresis(db_session, fake, namespace=PROBE_FAILED_NAMESPACE, ok=False, threshold=3)  # type: ignore[arg-type]
    gated = await _apply_failure_hysteresis(db_session, fake, namespace=PROBE_FAILED_NAMESPACE, ok=True, threshold=3)  # type: ignore[arg-type]
    assert gated is True
    counter = await control_plane_state_store.get_value(db_session, PROBE_FAILED_NAMESPACE, "dev-1")
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
    from app.core.leader import state_store as control_plane_state_store
    from app.devices.services.connectivity import IP_PING_NAMESPACE

    device = await make_device(connection_type="usb", ip_address="10.0.0.7")
    settings = _stub_settings(monkeypatch, threshold=3, timeout=2.0, count=1)
    _stub_get_health(monkeypatch, healthy_payload(adb=True, ip_ping=False))
    _stub_agent_devices(monkeypatch, {device.identity_value})

    await _run_connectivity_fold(
        ConnectivityService(
            publisher=Mock(),
            settings=settings,
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=DeviceHealthService(publisher=Mock()),
        ),
        db_session,
    )

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
    from app.core.leader import state_store as control_plane_state_store
    from app.devices.services.connectivity import IP_PING_NAMESPACE

    device = await make_device(connection_type="usb", ip_address="10.0.0.7")
    settings = _stub_settings(monkeypatch, threshold=3, timeout=2.0, count=1)
    _stub_get_health(monkeypatch, healthy_payload(adb=True, ip_ping=False))
    _stub_agent_devices(monkeypatch, {device.identity_value})
    handler_calls: list[str] = []
    mock_lifecycle_policy = MagicMock()
    mock_lifecycle_policy.handle_health_failure = _async_recorder(handler_calls)
    mock_lifecycle_policy.clear_escalation_residue_on_self_heal = AsyncMock(return_value=False)
    mock_lifecycle_policy.restore_run_after_self_heal = AsyncMock(return_value=False)

    for _ in range(3):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=settings,
                circuit_breaker=Mock(),
                lifecycle_policy=mock_lifecycle_policy,
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

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
    from app.core.leader import state_store as control_plane_state_store
    from app.devices.services.connectivity import IP_PING_NAMESPACE

    device = await make_device(connection_type="usb", ip_address="10.0.0.7")
    settings = _stub_settings(monkeypatch, threshold=3, timeout=2.0, count=1)
    _stub_agent_devices(monkeypatch, {device.identity_value})
    payloads: list[object] = [
        healthy_payload(adb=True, ip_ping=False),
        healthy_payload(adb=True, ip_ping=False),
        healthy_payload(adb=True, ip_ping=True),
    ]
    _stub_get_health_sequence(monkeypatch, payloads)

    for _ in range(3):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=settings,
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

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
    from app.core.leader import state_store as control_plane_state_store
    from app.devices.services.connectivity import IP_PING_NAMESPACE

    device = await make_device(connection_type="usb", ip_address="10.0.0.7")
    settings = _stub_settings(monkeypatch, threshold=3, timeout=2.0, count=1)
    _stub_get_health(monkeypatch, healthy_payload(adb=False, ip_ping=True))
    _stub_agent_devices(monkeypatch, {device.identity_value})

    await _run_connectivity_fold(
        ConnectivityService(
            publisher=Mock(),
            settings=settings,
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=DeviceHealthService(publisher=Mock()),
        ),
        db_session,
    )

    refreshed = await _reload(db_session, device.id)
    assert refreshed.device_checks_healthy is False
    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, device.identity_value)
    assert counter is None


# ---------------------------------------------------------------------------
# Debounceable health-check hysteresis — e.g. Roku ECP.
# A transient blip on a debounceable check must not flip the device unhealthy
# (and so must not auto-stop / exclude it from a run) on the first cycle.
# ---------------------------------------------------------------------------


def _roku_payload(*, reachable: bool) -> dict[str, object]:
    """A Roku-shaped health result: ping+ecp both reflect TCP reachability to ECP 8060."""
    detail = "" if reachable else "Roku ECP port 8060 unreachable"
    return {
        "healthy": reachable,
        "checks": [
            {"check_id": "ping", "ok": reachable, "message": detail, "debounce": True},
            {"check_id": "ecp", "ok": reachable, "message": detail, "debounce": True},
        ],
    }


def _debounce_settings(threshold: int = 3) -> FakeSettingsReader:
    return FakeSettingsReader(
        {
            "device_checks.ip_ping.consecutive_fail_threshold": 3,
            "device_checks.ip_ping.timeout_sec": 2.0,
            "device_checks.ip_ping.count_per_cycle": 1,
            "device_checks.probe_unanswered.consecutive_fail_threshold": 3,
            "device_checks.probe_failed.consecutive_fail_threshold": threshold,
        }
    )


@pytest.mark.asyncio
async def test_debounceable_check_blip_first_miss_keeps_healthy(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    """A single transient ECP blip (debounceable) must NOT flip unhealthy nor auto-stop."""
    device = await make_device(connection_type="network", ip_address="10.0.0.50")
    _stub_agent_devices(monkeypatch, {device.identity_value})
    _stub_get_health(monkeypatch, _roku_payload(reachable=False))
    handler_calls: list[str] = []
    mock_lifecycle_policy = MagicMock()
    mock_lifecycle_policy.handle_health_failure = _async_recorder(handler_calls)
    mock_lifecycle_policy.clear_escalation_residue_on_self_heal = AsyncMock(return_value=False)
    mock_lifecycle_policy.restore_run_after_self_heal = AsyncMock(return_value=False)

    await _run_connectivity_fold(
        ConnectivityService(
            publisher=Mock(),
            settings=_debounce_settings(threshold=3),
            circuit_breaker=Mock(),
            lifecycle_policy=mock_lifecycle_policy,
            health=DeviceHealthService(publisher=Mock()),
        ),
        db_session,
    )

    refreshed = await _reload(db_session, device.id)
    assert refreshed.device_checks_healthy is True
    assert handler_calls == []


@pytest.mark.asyncio
async def test_debounceable_check_flips_unhealthy_at_threshold(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    """Sustained ECP failure (>= threshold cycles) does flip unhealthy and auto-stops once."""
    device = await make_device(connection_type="network", ip_address="10.0.0.50")
    _stub_agent_devices(monkeypatch, {device.identity_value})
    _stub_get_health(monkeypatch, _roku_payload(reachable=False))
    handler_calls: list[str] = []
    mock_lifecycle_policy = MagicMock()
    mock_lifecycle_policy.handle_health_failure = _async_recorder(handler_calls)
    mock_lifecycle_policy.clear_escalation_residue_on_self_heal = AsyncMock(return_value=False)
    mock_lifecycle_policy.restore_run_after_self_heal = AsyncMock(return_value=False)

    for _ in range(3):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=_debounce_settings(threshold=3),
                circuit_breaker=Mock(),
                lifecycle_policy=mock_lifecycle_policy,
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    refreshed = await _reload(db_session, device.id)
    assert refreshed.device_checks_healthy is False
    assert len(handler_calls) == 1


@pytest.mark.asyncio
async def test_debounceable_check_recovery_clears_counter(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    """A passing cycle between blips resets the counter so the device never flips."""
    from app.core.leader import state_store as control_plane_state_store
    from app.devices.services.connectivity import PROBE_FAILED_NAMESPACE

    device = await make_device(connection_type="network", ip_address="10.0.0.50")
    _stub_agent_devices(monkeypatch, {device.identity_value})
    _stub_get_health_sequence(
        monkeypatch,
        [_roku_payload(reachable=False), _roku_payload(reachable=False), _roku_payload(reachable=True)],
    )
    mock_lifecycle_policy = MagicMock()
    mock_lifecycle_policy.handle_health_failure = AsyncMock()
    mock_lifecycle_policy.clear_escalation_residue_on_self_heal = AsyncMock(return_value=False)
    mock_lifecycle_policy.restore_run_after_self_heal = AsyncMock(return_value=False)
    mock_lifecycle_policy.attempt_auto_recovery = AsyncMock(return_value=False)

    for _ in range(3):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=_debounce_settings(threshold=3),
                circuit_breaker=Mock(),
                lifecycle_policy=mock_lifecycle_policy,
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    refreshed = await _reload(db_session, device.id)
    assert refreshed.device_checks_healthy is True
    mock_lifecycle_policy.handle_health_failure.assert_not_awaited()
    counter = await control_plane_state_store.get_value(db_session, PROBE_FAILED_NAMESPACE, device.identity_value)
    assert counter is None


@pytest.mark.asyncio
async def test_hard_check_failure_flips_immediately_despite_debounceable_sibling(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    """A non-debounceable (hard) check failing in the same cycle must flip on cycle 1."""
    device = await make_device(connection_type="network", ip_address="10.0.0.50")
    _stub_agent_devices(monkeypatch, {device.identity_value})
    # ecp is debounceable; adb is not — a hard failure must not be suppressed.
    _stub_get_health(
        monkeypatch,
        {
            "healthy": False,
            "checks": [
                {"check_id": "ecp", "ok": False, "message": "Roku ECP port 8060 unreachable", "debounce": True},
                {"check_id": "adb", "ok": False, "message": "adb dead"},
            ],
        },
    )
    handler_calls: list[str] = []
    mock_lifecycle_policy = MagicMock()
    mock_lifecycle_policy.handle_health_failure = _async_recorder(handler_calls)
    mock_lifecycle_policy.clear_escalation_residue_on_self_heal = AsyncMock(return_value=False)
    mock_lifecycle_policy.restore_run_after_self_heal = AsyncMock(return_value=False)

    await _run_connectivity_fold(
        ConnectivityService(
            publisher=Mock(),
            settings=_debounce_settings(threshold=3),
            circuit_breaker=Mock(),
            lifecycle_policy=mock_lifecycle_policy,
            health=DeviceHealthService(publisher=Mock()),
        ),
        db_session,
    )

    refreshed = await _reload(db_session, device.id)
    assert refreshed.device_checks_healthy is False
    assert len(handler_calls) == 1


@pytest.mark.asyncio
async def test_ip_ping_absent_no_counter_writes(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    from app.core.leader import state_store as control_plane_state_store
    from app.devices.services.connectivity import IP_PING_NAMESPACE

    device = await make_device(connection_type="usb", ip_address=None)
    settings = _stub_settings(monkeypatch, threshold=3, timeout=2.0, count=1)
    _stub_get_health(monkeypatch, healthy_payload(adb=True))  # no ip_ping entry
    _stub_agent_devices(monkeypatch, {device.identity_value})

    await _run_connectivity_fold(
        ConnectivityService(
            publisher=Mock(),
            settings=settings,
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=DeviceHealthService(publisher=Mock()),
        ),
        db_session,
    )

    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, device.identity_value)
    assert counter is None


@pytest.mark.asyncio
async def test_ip_ping_skipped_for_held_device(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    from app.core.leader import state_store as control_plane_state_store
    from app.devices.services.connectivity import IP_PING_NAMESPACE

    device = await make_device(
        connection_type="usb",
        ip_address="10.0.0.7",
        operational_state=DeviceOperationalState.maintenance,
        lifecycle_policy_state={"maintenance_reason": "test maintenance"},
    )
    settings = _stub_settings(monkeypatch, threshold=3, timeout=2.0, count=1)
    _stub_get_health(monkeypatch, healthy_payload(adb=True, ip_ping=False))
    _stub_agent_devices(monkeypatch, {device.identity_value})

    await _run_connectivity_fold(
        ConnectivityService(
            publisher=Mock(),
            settings=settings,
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=DeviceHealthService(publisher=Mock()),
        ),
        db_session,
    )

    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, device.identity_value)
    assert counter is None


@pytest.mark.asyncio
async def test_ip_ping_health_result_none_preserves_counter(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    from app.core.leader import state_store as control_plane_state_store
    from app.devices.services.connectivity import IP_PING_NAMESPACE

    device = await make_device(connection_type="usb", ip_address="10.0.0.7")
    settings = _stub_settings(monkeypatch, threshold=3, timeout=2.0, count=1)
    _stub_agent_devices(monkeypatch, {device.identity_value})
    await control_plane_state_store.set_value(db_session, IP_PING_NAMESPACE, device.identity_value, 2)
    await db_session.commit()
    _stub_get_health(monkeypatch, None)  # agent unreachable

    await _run_connectivity_fold(
        ConnectivityService(
            publisher=Mock(),
            settings=settings,
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=DeviceHealthService(publisher=Mock()),
        ),
        db_session,
    )

    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, device.identity_value)
    assert counter == 2


@pytest.mark.asyncio
async def test_ip_ping_settings_threshold_one_flips_immediately(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Coroutine[Any, Any, Device]],
) -> None:
    device = await make_device(connection_type="usb", ip_address="10.0.0.7")
    settings = _stub_settings(monkeypatch, threshold=1, timeout=2.0, count=1)
    _stub_get_health(monkeypatch, healthy_payload(adb=True, ip_ping=False))
    _stub_agent_devices(monkeypatch, {device.identity_value})

    await _run_connectivity_fold(
        ConnectivityService(
            publisher=Mock(),
            settings=settings,
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=DeviceHealthService(publisher=Mock()),
        ),
        db_session,
    )

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
    from app.core.leader import state_store as control_plane_state_store
    from app.devices.services.connectivity import CONNECTIVITY_NAMESPACE, IP_PING_NAMESPACE
    from app.devices.services.service import DeviceCrudService
    from tests.fakes import FakeSettingsReader

    device = await make_device(connection_type="usb", ip_address="10.0.0.7")
    await control_plane_state_store.set_value(db_session, IP_PING_NAMESPACE, device.identity_value, 2)
    await control_plane_state_store.set_value(db_session, CONNECTIVITY_NAMESPACE, device.identity_value, True)
    await db_session.commit()

    crud = DeviceCrudService(
        settings=FakeSettingsReader(), identity=DeviceIdentityConflictService(), publisher=event_bus
    )
    deleted = await crud.delete_device(db_session, device.id)
    assert deleted is True

    ip_ping_counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, device.identity_value)
    assert ip_ping_counter is None

    connectivity_flag = await control_plane_state_store.get_value(
        db_session, CONNECTIVITY_NAMESPACE, device.identity_value
    )
    assert connectivity_flag is None


# ── WI-6: lazy presence — direct probe first, enumeration only on miss ──────


async def test_healthy_probe_skips_enumeration(db_session: AsyncSession) -> None:
    """A device answering its own health probe is present; no discovery sweep runs."""
    _host, device, _ = await _setup_host_and_device(db_session)

    with (
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value={"dc-001"},
        ) as enumeration,
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available
    enumeration.assert_not_awaited()


async def test_healthy_probe_trumps_enumeration_absence(db_session: AsyncSession) -> None:
    """A healthy direct probe is presence even when the enumeration misses the device."""
    _host, device, _ = await _setup_host_and_device(db_session)

    mock_lifecycle_policy = MagicMock()
    mock_lifecycle_policy.note_connectivity_loss = AsyncMock()
    mock_lifecycle_policy.handle_health_failure = AsyncMock()
    mock_lifecycle_policy.attempt_auto_recovery = AsyncMock(return_value=False)
    mock_lifecycle_policy.clear_escalation_residue_on_self_heal = AsyncMock(return_value=False)
    mock_lifecycle_policy.restore_run_after_self_heal = AsyncMock(return_value=False)

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=mock_lifecycle_policy,
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available
    mock_lifecycle_policy.note_connectivity_loss.assert_not_awaited()


async def test_unhealthy_probe_absent_marks_disconnected_after_probe(db_session: AsyncSession) -> None:
    """Absent device: the direct probe runs first (and fails), then the enumeration confirms absence."""
    _host, _device, _ = await _setup_host_and_device(db_session)

    mock_lifecycle_policy = MagicMock()
    mock_lifecycle_policy.note_connectivity_loss = AsyncMock()
    mock_lifecycle_policy.handle_health_failure = AsyncMock()

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": False, "checks": [{"check_id": "adb", "ok": False}]},
        ) as health,
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=mock_lifecycle_policy,
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    health.assert_awaited_once()
    mock_lifecycle_policy.note_connectivity_loss.assert_awaited_once()
    mock_lifecycle_policy.handle_health_failure.assert_not_awaited()


async def test_unhealthy_probe_present_marks_health_failure(db_session: AsyncSession) -> None:
    """Present-but-unhealthy keeps the health-failure path (not the disconnected path)."""
    _host, _device, _ = await _setup_host_and_device(db_session)

    mock_lifecycle_policy = MagicMock()
    mock_lifecycle_policy.note_connectivity_loss = AsyncMock()
    mock_lifecycle_policy.handle_health_failure = AsyncMock()

    with (
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value={"dc-001"},
        ),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": False, "checks": [{"check_id": "adb", "ok": False}]},
        ),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=mock_lifecycle_policy,
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    mock_lifecycle_policy.handle_health_failure.assert_awaited_once()
    mock_lifecycle_policy.note_connectivity_loss.assert_not_awaited()


async def test_enumeration_fetched_once_per_host_for_multiple_misses(db_session: AsyncSession) -> None:
    """Two failing devices on one host share a single enumeration fetch."""
    host, _device, _ = await _setup_host_and_device(db_session)
    second = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="dc-002",
        connection_target="dc-002",
        name="Second Phone",
        os_version="14",
        host_id=host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(second)
    await db_session.commit()

    with (
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value=set(),
        ) as enumeration,
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": False, "checks": [{"check_id": "adb", "ok": False}]},
        ),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    enumeration.assert_awaited_once()


async def test_agent_unreachable_skips_remaining_devices(db_session: AsyncSession) -> None:
    """Enumeration None (agent unreachable) aborts the host without state writes."""
    _host, device, _ = await _setup_host_and_device(db_session)

    mock_lifecycle_policy = MagicMock()
    mock_lifecycle_policy.note_connectivity_loss = AsyncMock()
    mock_lifecycle_policy.handle_health_failure = AsyncMock()

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=None),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=mock_lifecycle_policy,
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available
    mock_lifecycle_policy.note_connectivity_loss.assert_not_awaited()
    mock_lifecycle_policy.handle_health_failure.assert_not_awaited()


async def test_healthy_non_offline_device_clears_previously_offline_flag(db_session: AsyncSession) -> None:
    """A healthy available device must clear a stale previously-offline flag so a
    later genuine recovery picks the startup-recovery reason."""
    _host, device, _ = await _setup_host_and_device(db_session)
    await track_previously_offline_device(db_session, "dc-001")

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True},
        ),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available
    assert "dc-001" not in await get_connectivity_control_plane_state(db_session)


async def test_absent_device_hard_failure_does_not_touch_ip_ping_counter(db_session: AsyncSession) -> None:
    """A disconnected device failing hard checks must not skew the ip_ping
    hysteresis counter or metrics — the verdict is unhealthy regardless."""
    from app.core.leader import state_store as control_plane_state_store
    from app.devices.services.connectivity import IP_PING_NAMESPACE

    _host, _device, _ = await _setup_host_and_device(db_session)

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            return_value={
                "healthy": False,
                "checks": [{"check_id": "adb", "ok": False}, {"check_id": "ip_ping", "ok": False}],
            },
        ),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    counter = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, "dc-001")
    assert counter is None


# ---------------------------------------------------------------------------
# Adapter-recommended link repair (Task 7)
# ---------------------------------------------------------------------------

_RESOLVED_WITH_RECONNECT = SimpleNamespace(lifecycle_actions=[SimpleNamespace(id="reconnect")], health_checks=[])


async def _device_event_types(db_session: AsyncSession, device_id: uuid.UUID) -> list[str]:
    from sqlalchemy import select as _select

    from app.devices.models.event import DeviceEvent

    rows = (await db_session.execute(_select(DeviceEvent).where(DeviceEvent.device_id == device_id))).scalars().all()
    return [r.event_type.value for r in rows]


def _recommend_repair_patches() -> tuple[object, object]:
    return (
        patch(
            "app.devices.services.connectivity.resolve_pack_platform",
            new=AsyncMock(return_value=_RESOLVED_WITH_RECONNECT),
        ),
        patch("app.devices.services.connectivity.platform_has_lifecycle_action", new=Mock(return_value=True)),
    )


async def test_recommended_action_dispatches_repair_for_available_device(db_session: AsyncSession) -> None:
    """Probe returns unhealthy + recommended_action=reconnect; loop dispatches the
    action and, when the re-probe is healthy, the device stays available."""
    _host, device, _ = await _setup_host_and_device(db_session, with_node=True)
    unhealthy = {
        "healthy": False,
        "checks": [{"check_id": "adb_connected", "ok": False}],
        "recommended_action": "reconnect",
    }
    dispatch = AsyncMock(return_value={"success": True})
    rp, ph = _recommend_repair_patches()
    with (
        rp,
        ph,
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            side_effect=[unhealthy, {"healthy": True}],
        ),
        patch("app.devices.services.link_repair.dispatch_recommended_action", new=dispatch),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    dispatch.assert_awaited_once()
    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available
    assert "repair_attempted" in await _device_event_types(db_session, device.id)


async def test_repair_reprobe_missing_healthy_key_is_not_treated_as_recovered(db_session: AsyncSession) -> None:
    """BUG-2: after a successful repair dispatch, a malformed/empty re-probe (no
    ``healthy`` key, no non-ip_ping checks) must NOT be read as healthy — otherwise
    the device is declared recovered with a still-dead link and the repair budget is
    reset. Conservative default: missing positive evidence => not healthy."""
    from app.core.leader import state_store
    from app.devices.services.link_repair import REPAIR_ATTEMPTS_NAMESPACE

    _host, device, _ = await _setup_host_and_device(db_session, with_node=True)
    unhealthy = {
        "healthy": False,
        "checks": [{"check_id": "adb_connected", "ok": False}],
        "recommended_action": "reconnect",
    }
    dispatch = AsyncMock(return_value={"success": True})
    rp, ph = _recommend_repair_patches()
    with (
        rp,
        ph,
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            side_effect=[unhealthy, {}],  # re-probe came back empty
        ),
        patch("app.devices.services.link_repair.dispatch_recommended_action", new=dispatch),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    dispatch.assert_awaited_once()
    # Budget consumed and NOT reset => the empty re-probe was not treated as recovery.
    counter = await state_store.get_value(db_session, REPAIR_ATTEMPTS_NAMESPACE, device.identity_value)
    assert counter == 1


async def test_recommended_action_dispatches_for_offline_wedged_device(db_session: AsyncSession) -> None:
    """The wedge case: device already offline, probe still recommends reconnect →
    dispatch still happens (this is what un-wedges it)."""
    _host, device, _ = await _setup_host_and_device(
        db_session, device_operational_state=DeviceOperationalState.offline, with_node=True
    )
    unhealthy = {
        "healthy": False,
        "checks": [{"check_id": "adb_connected", "ok": False}],
        "recommended_action": "reconnect",
    }
    dispatch = AsyncMock(return_value={"success": True})
    rp, ph = _recommend_repair_patches()
    with (
        rp,
        ph,
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            side_effect=[unhealthy, None],
        ),
        patch("app.devices.services.link_repair.dispatch_recommended_action", new=dispatch),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    dispatch.assert_awaited_once()
    assert "repair_attempted" in await _device_event_types(db_session, device.id)


async def test_repair_not_dispatched_when_pack_draining(db_session: AsyncSession) -> None:
    from sqlalchemy import update as _update

    from app.packs.models.pack import DriverPack, PackState

    _host, device, _ = await _setup_host_and_device(db_session, with_node=True)
    await db_session.execute(
        _update(DriverPack).where(DriverPack.id == device.pack_id).values(state=PackState.draining)
    )
    await db_session.commit()
    unhealthy = {
        "healthy": False,
        "checks": [{"check_id": "adb_connected", "ok": False}],
        "recommended_action": "reconnect",
    }
    dispatch = AsyncMock(return_value={"success": True})
    # No resolver patch: the draining guarantee now rests on resolve_pack_platform
    # filtering to enabled packs (PackPlatformNotFound -> repair skipped).
    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            side_effect=[unhealthy],
        ),
        patch("app.devices.services.link_repair.dispatch_recommended_action", new=dispatch),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    dispatch.assert_not_awaited()


async def test_repair_budget_exhausts_then_records_failed(db_session: AsyncSession) -> None:
    from app.core.leader import state_store
    from app.devices.services.link_repair import REPAIR_ATTEMPTS_NAMESPACE, REPAIR_MAX_ATTEMPTS

    _host, device, _ = await _setup_host_and_device(db_session, with_node=True)
    await state_store.set_value(db_session, REPAIR_ATTEMPTS_NAMESPACE, device.identity_value, REPAIR_MAX_ATTEMPTS)
    await db_session.commit()
    unhealthy = {
        "healthy": False,
        "checks": [{"check_id": "adb_connected", "ok": False}],
        "recommended_action": "reconnect",
    }
    dispatch = AsyncMock(return_value={"success": True})
    rp, ph = _recommend_repair_patches()
    with (
        rp,
        ph,
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            side_effect=[unhealthy],
        ),
        patch("app.devices.services.link_repair.dispatch_recommended_action", new=dispatch),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    dispatch.assert_not_awaited()
    assert "repair_failed" in await _device_event_types(db_session, device.id)


# ---------------------------------------------------------------------------
# Unanswered-probe fact (Task 8)
# ---------------------------------------------------------------------------


async def test_unanswered_probe_marks_unhealthy_on_threshold(db_session: AsyncSession) -> None:
    """N consecutive None probes (AgentCallError) → device marked unhealthy and
    handle_health_failure invoked, instead of being silently skipped."""
    _host, _device, _ = await _setup_host_and_device(db_session, with_node=True)
    lifecycle_policy = AsyncMock()
    svc = ConnectivityService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        lifecycle_policy=lifecycle_policy,
        health=DeviceHealthService(publisher=Mock()),
    )
    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=None),
        patch("app.devices.services.connectivity._get_device_health", new_callable=AsyncMock, return_value=None),
    ):
        await _run_connectivity_fold(svc, db_session)
        await _run_connectivity_fold(svc, db_session)
        lifecycle_policy.handle_health_failure.assert_not_awaited()  # 2 of 3, below threshold
        await _run_connectivity_fold(svc, db_session)

    lifecycle_policy.handle_health_failure.assert_awaited()  # threshold reached


async def test_answered_probe_resets_unanswered_counter(db_session: AsyncSession) -> None:
    """An answered probe resets the unanswered counter so isolated probe errors
    never accumulate to the threshold."""
    _host, _device, _ = await _setup_host_and_device(db_session, with_node=True)
    lifecycle_policy = AsyncMock()
    svc = ConnectivityService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        lifecycle_policy=lifecycle_policy,
        health=DeviceHealthService(publisher=Mock()),
    )
    # None, None, healthy (reset), None -> never reaches threshold of 3
    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=None),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            side_effect=[None, None, {"healthy": True}, None],
        ),
    ):
        for _ in range(4):
            await _run_connectivity_fold(svc, db_session)

    lifecycle_policy.handle_health_failure.assert_not_awaited()


async def test_disconnected_already_recorded_device_skips_reconcile(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy device already recorded disconnected (device_checks_healthy=False, no
    running node) must not re-mark dirty every cycle while it stays disconnected."""
    _host, device, _ = await _setup_host_and_device(
        db_session,
        connection_target="dc-gate-001",
        device_operational_state=DeviceOperationalState.busy,
        with_node=False,
    )
    device.device_checks_healthy = False
    await db_session.commit()

    from app.devices.services.intent import IntentService

    calls: list[str] = []

    async def _spy(self: object, device_id: object, **kwargs: object) -> None:
        calls.append("called")

    monkeypatch.setattr(IntentService, "reconcile_now", _spy)

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch("app.devices.services.connectivity._get_device_health", new_callable=AsyncMock, return_value=None),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({"device_checks.probe_unanswered.consecutive_fail_threshold": 5}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    assert calls == []  # already-recorded disconnect did not re-enqueue


async def test_disconnected_first_observation_still_reconciles(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First disconnect (device_checks_healthy not yet False) must still be handled."""
    _host, device, _ = await _setup_host_and_device(
        db_session,
        connection_target="dc-gate-002",
        device_operational_state=DeviceOperationalState.busy,
        with_node=False,
    )
    device.device_checks_healthy = True  # was healthy → this cycle is the transition
    await db_session.commit()

    from app.devices.services.intent import IntentService

    calls: list[str] = []

    async def _spy(self: object, device_id: object, **kwargs: object) -> None:
        calls.append("called")

    monkeypatch.setattr(IntentService, "reconcile_now", _spy)

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set()),
        patch("app.devices.services.connectivity._get_device_health", new_callable=AsyncMock, return_value=None),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({"device_checks.probe_unanswered.consecutive_fail_threshold": 5}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    assert len(calls) >= 1  # the connected→disconnected transition was handled


async def test_probe_passes_claimed_ports_and_live_flag(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _get_device_health must forward the new facts to fetch_pack_device_health
    _host, device, _ = await _setup_host_and_device(db_session)
    seen: dict[str, Any] = {}

    async def fake_fetch(*a: object, **kw: object) -> dict[str, Any]:
        seen.update(kw)
        return {"healthy": True, "checks": []}

    monkeypatch.setattr("app.devices.services.connectivity.fetch_pack_device_health", fake_fetch)
    await _get_device_health(
        device,
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        claimed_ports={"appium:systemPort": 8200},
        has_live_session=False,
    )
    assert seen["claimed_ports"] == {"appium:systemPort": 8200}
    assert seen["has_live_session"] is False


async def _device_event_payload(db_session: AsyncSession, device_id: uuid.UUID, event_type: str) -> dict[str, Any]:
    from sqlalchemy import select as _select

    from app.devices.models.event import DeviceEvent

    rows = (await db_session.execute(_select(DeviceEvent).where(DeviceEvent.device_id == device_id))).scalars().all()
    for row in rows:
        if row.event_type.value == event_type:
            return dict(row.details or {})
    raise AssertionError(f"no {event_type} event for {device_id}")


async def test_repair_event_records_curing_rung(db_session: AsyncSession) -> None:
    # dispatch succeeds with a rung detail; re-probe healthy → repair_attempted
    # event carries that detail.
    _host, device, _ = await _setup_host_and_device(db_session, with_node=True)
    unhealthy = {
        "healthy": False,
        "checks": [{"check_id": "claimed_ports_free", "ok": False}],
        "recommended_action": "release_forwarded_ports",
    }
    dispatch = AsyncMock(return_value={"success": True, "detail": "cured_by=forward_remove"})
    rp, ph = _recommend_repair_patches()
    with (
        rp,
        ph,
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value={"dc-001"}),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new_callable=AsyncMock,
            side_effect=[unhealthy, {"healthy": True, "checks": [{"check_id": "claimed_ports_free", "ok": True}]}],
        ),
        patch("app.devices.services.link_repair.dispatch_recommended_action", new=dispatch),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=Mock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=DeviceHealthService(publisher=Mock()),
            ),
            db_session,
        )

    dispatch.assert_awaited_once()
    payload = await _device_event_payload(db_session, device.id, "repair_attempted")
    assert payload["detail"] == "cured_by=forward_remove"
