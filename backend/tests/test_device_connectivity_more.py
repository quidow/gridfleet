from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AgentCallError
from app.models.appium_node import AppiumNode, NodeState
from app.models.device import ConnectionType, Device, DeviceAvailabilityStatus, DeviceType
from app.models.host import Host, HostStatus, OSType
from app.services import device_connectivity
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
    return Device(
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
        availability_status=DeviceAvailabilityStatus.available,
        device_type=device_type,
        connection_type=ConnectionType.usb,
        host=host,
    )


async def test_get_device_health_returns_none_for_missing_host_or_agent_errors() -> None:
    device = _device()
    device.host = None
    assert await device_connectivity._get_device_health(device) is None

    device = _device()
    with patch(
        "app.services.device_connectivity.fetch_pack_device_health",
        new=AsyncMock(side_effect=AgentCallError("10.0.0.10", "boom")),
    ):
        assert await device_connectivity._get_device_health(device) is None


async def test_get_agent_devices_returns_none_when_agent_call_fails() -> None:
    host = _device().host
    assert host is not None

    with patch(
        "app.services.device_connectivity.get_pack_devices",
        new=AsyncMock(side_effect=AgentCallError("10.0.0.10", "boom")),
    ):
        assert await device_connectivity._get_agent_devices(host) is None


async def test_get_lifecycle_state_handles_declared_actions_and_failures() -> None:
    emulator = _device(device_type=DeviceType.emulator)
    real = _device()
    db = AsyncMock()

    with (
        patch(
            "app.services.device_connectivity.resolve_pack_platform",
            new=AsyncMock(return_value=SimpleNamespace(lifecycle_actions=[{"id": "state"}])),
        ),
        patch(
            "app.services.device_connectivity.pack_device_lifecycle_action",
            new=AsyncMock(return_value={"state": "booted"}),
        ),
    ):
        assert await device_connectivity._get_lifecycle_state(db, emulator) == "booted"

    with patch(
        "app.services.device_connectivity.resolve_pack_platform",
        new=AsyncMock(return_value=SimpleNamespace(lifecycle_actions=[])),
    ):
        assert await device_connectivity._get_lifecycle_state(db, real) is None

    with (
        patch(
            "app.services.device_connectivity.resolve_pack_platform",
            new=AsyncMock(return_value=SimpleNamespace(lifecycle_actions=[{"id": "state"}])),
        ),
        patch(
            "app.services.device_connectivity.pack_device_lifecycle_action",
            new=AsyncMock(side_effect=AgentCallError("10.0.0.10", "boom")),
        ),
    ):
        assert await device_connectivity._get_lifecycle_state(db, emulator) is None

    emulator.connection_target = None
    with patch(
        "app.services.device_connectivity.resolve_pack_platform",
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


async def test_connected_offline_device_clears_control_plane_state_when_not_ready_or_not_auto_managed(
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
    auto_manage_off = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="manual-device",
        connection_target="manual-device",
        name="Manual Device",
    )
    not_ready.availability_status = DeviceAvailabilityStatus.offline
    auto_manage_off.availability_status = DeviceAvailabilityStatus.offline
    auto_manage_off.auto_manage = False
    await db_session.commit()

    with (
        patch(
            "app.services.device_connectivity._get_agent_devices",
            new=AsyncMock(return_value={"not-ready", "manual-device"}),
        ),
        patch(
            "app.services.device_connectivity._get_device_health",
            new=AsyncMock(return_value={"healthy": True}),
        ),
        patch(
            "app.services.device_connectivity.control_plane_state_store.delete_value",
            new=AsyncMock(),
        ) as delete_value,
    ):
        await device_connectivity._check_connectivity(db_session)

    assert delete_value.await_count == 2


async def test_virtual_device_connectivity_updates_emulator_state_and_non_managed_disconnect_is_skipped(
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
    emulator.availability_status = DeviceAvailabilityStatus.available
    manual = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="manual-offline",
        connection_target="manual-offline",
        name="Manual",
    )
    manual.auto_manage = False
    await db_session.commit()

    with (
        patch("app.services.device_connectivity._get_agent_devices", new=AsyncMock(return_value={"emu-1"})),
        patch("app.services.device_connectivity._get_lifecycle_state", new=AsyncMock(return_value="booted")),
        patch("app.services.device_connectivity._get_device_health", new=AsyncMock(return_value={"healthy": True})),
        patch(
            "app.services.device_connectivity.device_health.update_emulator_state",
            new=AsyncMock(),
        ) as update_emulator_state,
        patch("app.services.device_connectivity.record_event", new=AsyncMock()) as record_event,
    ):
        await device_connectivity._check_connectivity(db_session)

    assert any(call.args[2] == "booted" for call in update_emulator_state.await_args_list)
    record_event.assert_not_awaited()


async def test_stop_node_via_agent_delegates_to_helper() -> None:
    device = _device()
    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://hub", state=NodeState.running)

    with patch(
        "app.services.device_connectivity.stop_node_via_agent_helper", new=AsyncMock(return_value=True)
    ) as helper:
        result = await device_connectivity._stop_node_via_agent(device, node)

    helper.assert_awaited_once()
    assert result is True


async def test_device_connectivity_loop_logs_and_retries() -> None:
    class _Observation:
        @asynccontextmanager
        async def cycle(self) -> AsyncMock:
            yield AsyncMock()

    @asynccontextmanager
    async def fake_session() -> AsyncMock:
        yield AsyncMock()

    with (
        patch("app.services.device_connectivity.observe_background_loop", return_value=_Observation()),
        patch("app.services.device_connectivity.async_session", fake_session),
        patch(
            "app.services.device_connectivity._check_connectivity",
            new=AsyncMock(side_effect=[RuntimeError("boom"), asyncio.CancelledError()]),
        ),
        patch("app.services.device_connectivity.settings_service.get", return_value=1),
        patch("app.services.device_connectivity.asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(asyncio.CancelledError),
    ):
        await device_connectivity.device_connectivity_loop()

    sleep.assert_awaited()
