from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.devices.services import state_write_guard

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AgentCallError
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services import connectivity as device_connectivity
from app.devices.services import lifecycle_policy
from app.hosts.models import Host, HostStatus, OSType
from tests.helpers import create_device_record


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
    with state_write_guard.bypass():
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
    assert await device_connectivity._get_device_health(device) is None

    device = _device()
    with patch(
        "app.devices.services.connectivity.fetch_pack_device_health",
        new=AsyncMock(side_effect=AgentCallError("10.0.0.10", "boom")),
    ):
        assert await device_connectivity._get_device_health(device) is None


async def test_get_agent_devices_returns_none_when_agent_call_fails() -> None:
    host = _device().host
    assert host is not None

    with patch(
        "app.devices.services.connectivity.get_pack_devices",
        new=AsyncMock(side_effect=AgentCallError("10.0.0.10", "boom")),
    ):
        assert await device_connectivity._get_agent_devices(host) is None


async def test_get_lifecycle_state_handles_declared_actions_and_failures() -> None:
    emulator = _device(device_type=DeviceType.emulator)
    real = _device()
    db = AsyncMock()

    with (
        patch(
            "app.devices.services.connectivity.resolve_pack_platform",
            new=AsyncMock(return_value=SimpleNamespace(lifecycle_actions=[{"id": "state"}])),
        ),
        patch(
            "app.devices.services.connectivity.pack_device_lifecycle_action",
            new=AsyncMock(return_value={"state": "booted"}),
        ),
    ):
        assert await device_connectivity._get_lifecycle_state(db, emulator) == "booted"

    with patch(
        "app.devices.services.connectivity.resolve_pack_platform",
        new=AsyncMock(return_value=SimpleNamespace(lifecycle_actions=[])),
    ):
        assert await device_connectivity._get_lifecycle_state(db, real) is None

    with (
        patch(
            "app.devices.services.connectivity.resolve_pack_platform",
            new=AsyncMock(return_value=SimpleNamespace(lifecycle_actions=[{"id": "state"}])),
        ),
        patch(
            "app.devices.services.connectivity.pack_device_lifecycle_action",
            new=AsyncMock(side_effect=AgentCallError("10.0.0.10", "boom")),
        ),
    ):
        assert await device_connectivity._get_lifecycle_state(db, emulator) is None

    emulator.connection_target = None
    with patch(
        "app.devices.services.connectivity.resolve_pack_platform",
        new=AsyncMock(return_value=SimpleNamespace(lifecycle_actions=[{"id": "state"}])),
    ):
        assert await device_connectivity._get_lifecycle_state(db, emulator) is None


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
    with state_write_guard.bypass():
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
        patch("app.devices.services.connectivity.assert_current_leader"),
    ):
        await device_connectivity._check_connectivity(db_session)

    assert delete_value.await_count == 1


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
    with state_write_guard.bypass():
        emulator.operational_state = DeviceOperationalState.available
    await db_session.commit()

    with (
        patch("app.devices.services.connectivity._get_agent_devices", new=AsyncMock(return_value={"emu-1"})),
        patch("app.devices.services.connectivity._get_lifecycle_state", new=AsyncMock(return_value="booted")),
        patch("app.devices.services.connectivity._get_device_health", new=AsyncMock(return_value={"healthy": True})),
        patch(
            "app.devices.services.connectivity.device_health.update_emulator_state",
            new=AsyncMock(),
        ) as update_emulator_state,
        patch("app.devices.services.connectivity.assert_current_leader"),
    ):
        await device_connectivity._check_connectivity(db_session)

    assert any(call.args[2] == "booted" for call in update_emulator_state.await_args_list)


async def test_device_connectivity_loop_logs_and_retries() -> None:
    class _Observation:
        @asynccontextmanager
        async def cycle(self) -> AsyncMock:
            yield AsyncMock()

    @asynccontextmanager
    async def fake_session() -> AsyncMock:
        yield AsyncMock()

    with (
        patch("app.devices.services.connectivity.observe_background_loop", return_value=_Observation()),
        patch("app.devices.services.connectivity.async_session", fake_session),
        patch(
            "app.devices.services.connectivity._check_connectivity",
            new=AsyncMock(side_effect=[RuntimeError("boom"), asyncio.CancelledError()]),
        ),
        patch("app.devices.services.connectivity._check_expired_cooldowns", new=AsyncMock(return_value=None)),
        patch("app.devices.services.connectivity._default_settings.get", return_value=1),
        patch("app.devices.services.connectivity.asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(asyncio.CancelledError),
    ):
        await device_connectivity.device_connectivity_loop()

    sleep.assert_awaited()


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
    with state_write_guard.bypass():
        device.operational_state = DeviceOperationalState.offline
    await db_session.commit()

    handle_health_failure_called = False
    original_handler = lifecycle_policy.handle_health_failure

    async def spy(db: AsyncSession, device: Device, *, source: str, reason: str) -> str:
        nonlocal handle_health_failure_called
        handle_health_failure_called = True
        return await original_handler(db, device, source=source, reason=reason)

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
        patch("app.devices.services.connectivity.assert_current_leader"),
        patch("app.devices.services.connectivity.lifecycle_policy.handle_health_failure", spy),
    ):
        await device_connectivity._check_connectivity(db_session)

    assert handle_health_failure_called is False, "handle_health_failure must not be called for already-offline device"
