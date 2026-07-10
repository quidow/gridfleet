from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AgentCallError
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services import connectivity as device_connectivity
from app.devices.services.connectivity import ConnectivityService
from app.hosts.models import Host, HostStatus, OSType
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device_record
from tests.helpers import test_event_bus as event_bus


async def _run_connectivity_fold(service: ConnectivityService, db: AsyncSession) -> None:
    """Fold the pushed device_health section once per online host — the fan-out
    the deleted check_connectivity pass used to do. Each device's observation is
    sourced from the (mocked) _get_device_health so the legacy per-device mocks
    keep working."""
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


def _device(
    *,
    device_type: DeviceType = DeviceType.real_device,
    platform_id: str = "android_mobile",
    pack_id: str = "appium-uiautomator2",
) -> Device:
    host = Host(
        id=uuid4(),
        hostname="connectivity-host",
        ip="10.0.0.10",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    device = Device(
        id=uuid4(),
        host_id=host.id,
        pack_id=pack_id,
        platform_id=platform_id,
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="demo",
        connection_target="demo",
        name="Demo",
        os_version="14",
        operational_state=DeviceOperationalState.available,
        device_type=device_type,
        connection_type=ConnectionType.usb,
        host=host,
    )
    return device


async def test_get_device_health_returns_none_for_missing_host_or_agent_errors() -> None:
    device = _device()
    device.host = None
    assert (
        await device_connectivity._get_device_health(device, settings=FakeSettingsReader(), circuit_breaker=Mock())
        is None
    )

    device = _device()
    with patch(
        "app.devices.services.connectivity.fetch_pack_device_health",
        new=AsyncMock(side_effect=AgentCallError("10.0.0.10", "boom")),
    ):
        assert (
            await device_connectivity._get_device_health(device, settings=FakeSettingsReader(), circuit_breaker=Mock())
            is None
        )


async def test_get_agent_devices_returns_none_when_agent_call_fails() -> None:
    host = _device().host
    assert host is not None

    with patch(
        "app.devices.services.connectivity.get_pack_devices",
        new=AsyncMock(side_effect=AgentCallError("10.0.0.10", "boom")),
    ):
        assert (
            await device_connectivity._get_agent_devices(host, settings=FakeSettingsReader({}), circuit_breaker=Mock())
            is None
        )


async def test_lifecycle_state_capable_and_fetch_handle_declared_actions_and_failures() -> None:
    emulator = _device(device_type=DeviceType.emulator)
    real = _device()
    db = AsyncMock()

    # Capability pre-pass (DB-only): "state" action declared => capable.
    with patch(
        "app.devices.services.connectivity.resolve_pack_platform",
        new=AsyncMock(return_value=SimpleNamespace(lifecycle_actions=[{"id": "state"}])),
    ):
        assert await device_connectivity._lifecycle_state_capable(db, emulator) is True

    with patch(
        "app.devices.services.connectivity.resolve_pack_platform",
        new=AsyncMock(return_value=SimpleNamespace(lifecycle_actions=[])),
    ):
        assert await device_connectivity._lifecycle_state_capable(db, real) is False

    with patch(
        "app.devices.services.connectivity.resolve_pack_platform",
        new=AsyncMock(side_effect=LookupError("no pack")),
    ):
        assert await device_connectivity._lifecycle_state_capable(db, emulator) is False

    # Fetch (pure agent I/O): happy path returns the reported state.
    with patch(
        "app.devices.services.connectivity.pack_device_lifecycle_action",
        new=AsyncMock(return_value={"state": "booted"}),
    ):
        assert (
            await device_connectivity._fetch_lifecycle_state(
                emulator, settings=FakeSettingsReader({}), circuit_breaker=Mock()
            )
            == "booted"
        )

    # Agent error => None.
    with patch(
        "app.devices.services.connectivity.pack_device_lifecycle_action",
        new=AsyncMock(side_effect=AgentCallError("10.0.0.10", "boom")),
    ):
        assert (
            await device_connectivity._fetch_lifecycle_state(
                emulator, settings=FakeSettingsReader({}), circuit_breaker=Mock()
            )
            is None
        )

    # No connection target => None without any agent call.
    emulator.connection_target = None
    assert (
        await device_connectivity._fetch_lifecycle_state(
            emulator, settings=FakeSettingsReader({}), circuit_breaker=Mock()
        )
        is None
    )


def test_summarize_unhealthy_result_covers_detail_and_failed_checks() -> None:
    assert device_connectivity._summarize_unhealthy_result(None) == "Device health checks failed"
    assert device_connectivity._summarize_unhealthy_result({"detail": "ADB not responsive"}) == "ADB not responsive"
    assert (
        device_connectivity._summarize_unhealthy_result(
            {
                "healthy": False,
                "checks": [
                    {"check_id": "adb_connected", "ok": False, "message": "device not found"},
                    {"check_id": "screen_visible", "ok": False, "message": "screen off"},
                ],
            }
        )
        == "Failed checks: adb connected, screen visible"
    )
    assert (
        device_connectivity._summarize_unhealthy_result({"healthy": True, "checks": []})
        == "Device health checks failed"
    )
    # No checks key → fallback
    assert device_connectivity._summarize_unhealthy_result({"healthy": False}) == "Device health checks failed"


async def test_connected_offline_device_clears_control_plane_state_when_not_ready(
    db_session: AsyncSession,
) -> None:
    host = Host(hostname="loop-host", ip="10.0.0.11", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()

    not_ready = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="not-ready",
        connection_target="not-ready",
        name="Not Ready",
        verified=False,
    )
    not_ready.operational_state = DeviceOperationalState.offline
    await db_session.commit()

    with (
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new=AsyncMock(return_value={"not-ready"}),
        ),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new=AsyncMock(return_value={"healthy": True}),
        ),
        patch(
            "app.devices.services.connectivity.control_plane_state_store.delete_value",
            new=AsyncMock(),
        ) as delete_value,
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=event_bus,
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=AsyncMock(),
            ),
            db_session,
        )

    # The healthy probe also clears the repair-attempt and probe-unanswered keys; this
    # test asserts the specific "previously offline" clear for the not-ready device.
    from app.devices.services.connectivity import CONNECTIVITY_NAMESPACE

    assert any(
        call.args[1] == CONNECTIVITY_NAMESPACE and call.args[2] == "not-ready" for call in delete_value.await_args_list
    )


async def test_virtual_device_connectivity_updates_emulator_state(
    db_session: AsyncSession,
) -> None:
    host = Host(hostname="emu-host", ip="10.0.0.12", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()

    emulator = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="emu-1",
        connection_target="emu-1",
        name="Emulator",
        device_type=DeviceType.emulator.value,
        connection_type=ConnectionType.virtual.value,
    )
    emulator.operational_state = DeviceOperationalState.available
    await db_session.commit()

    update_emulator_state = AsyncMock()
    health_stub = AsyncMock()
    health_stub.update_emulator_state = update_emulator_state
    with (
        patch("app.devices.services.connectivity._get_agent_devices", new=AsyncMock(return_value={"emu-1"})),
        patch("app.devices.services.connectivity._lifecycle_state_capable", new=AsyncMock(return_value=True)),
        patch("app.devices.services.connectivity._fetch_lifecycle_state", new=AsyncMock(return_value="booted")),
        patch("app.devices.services.connectivity._get_device_health", new=AsyncMock(return_value={"healthy": True})),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=event_bus,
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=health_stub,
            ),
            db_session,
        )

    assert any(call.args[2] == "booted" for call in update_emulator_state.await_args_list)


async def test_connectivity_loop_skips_handle_health_failure_for_offline_device(
    db_session: AsyncSession,
) -> None:
    """The connectivity loop must NOT call handle_health_failure for a device
    already in offline state — the crash already happened and calling the
    handler again emits a redundant device.crashed event on every tick.

    Exercises `_check_connectivity` end-to-end with mocked agent calls.
    """
    host = Host(
        hostname="offline-host", ip="10.0.0.20", os_type=OSType.linux, agent_port=5100, status=HostStatus.online
    )
    db_session.add(host)
    await db_session.flush()

    device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="already-offline-conn-1",
        connection_target="already-offline-conn-1",
        name="Already Offline Device",
    )
    device.operational_state = DeviceOperationalState.offline
    await db_session.commit()

    handle_health_failure_called = False

    async def spy(*args: object, **kwargs: object) -> str:
        nonlocal handle_health_failure_called
        handle_health_failure_called = True
        return ""

    mock_lifecycle_policy = AsyncMock()
    mock_lifecycle_policy.handle_health_failure = spy

    with (
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new=AsyncMock(return_value={"already-offline-conn-1"}),
        ),
        patch(
            "app.devices.services.connectivity._get_device_health",
            new=AsyncMock(
                return_value={
                    "healthy": False,
                    "checks": [
                        {"check_id": "adb_connected", "ok": False},
                        {"check_id": "adb_responsive", "ok": False},
                    ],
                }
            ),
        ),
    ):
        await _run_connectivity_fold(
            ConnectivityService(
                publisher=event_bus,
                settings=FakeSettingsReader({}),
                circuit_breaker=Mock(),
                lifecycle_policy=mock_lifecycle_policy,
                health=AsyncMock(),
            ),
            db_session,
        )

    assert handle_health_failure_called is False, "handle_health_failure must not be called for already-offline device"
