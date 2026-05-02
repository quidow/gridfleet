from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from adapter.lifecycle import lifecycle_action


class _Ctx:
    host_id = "h1"
    device_identity_value = "192.168.1.100:5555"


class _AvdCtx:
    host_id = "h1"
    device_identity_value = "Pixel_6"


@pytest.mark.asyncio
@patch("adapter.lifecycle._adb_shell_echo", new_callable=AsyncMock, return_value=True)
@patch("adapter.lifecycle.run_cmd", new_callable=AsyncMock, side_effect=["", "connected to 192.168.1.100:5555"])
async def test_reconnect_success(mock_cmd: AsyncMock, _mock_echo: AsyncMock) -> None:
    result = await lifecycle_action("reconnect", {"ip_address": "192.168.1.100"}, _Ctx())
    assert result.ok is True


@pytest.mark.asyncio
async def test_boot_running_avd_returns_active_adb_serial() -> None:
    with (
        patch("adapter.lifecycle.find_adb", return_value="adb"),
        patch("adapter.lifecycle.find_emulator", return_value="/sdk/emulator/emulator"),
        patch("adapter.lifecycle._running_serial_for_avd", new=AsyncMock(return_value="emulator-5554")),
        patch("adapter.lifecycle.asyncio.create_subprocess_exec", new_callable=AsyncMock) as create_proc,
    ):
        result = await lifecycle_action("boot", {}, _AvdCtx())

    assert result.ok is True
    assert result.state == "emulator-5554"
    create_proc.assert_not_called()


@pytest.mark.asyncio
async def test_state_resolves_running_avd_name() -> None:
    with (
        patch("adapter.lifecycle.find_adb", return_value="adb"),
        patch(
            "adapter.lifecycle.run_cmd",
            new=AsyncMock(side_effect=["", "List of devices attached\nemulator-5554\tdevice\n"]),
        ),
        patch("adapter.lifecycle.get_running_emulator_avd_name", new=AsyncMock(return_value="Pixel_6")),
    ):
        result = await lifecycle_action("state", {}, _AvdCtx())

    assert result.ok is True
    assert result.state == "running"


@pytest.mark.asyncio
async def test_state_maps_adb_device_state_to_running() -> None:
    ctx = _AvdCtx()
    ctx.device_identity_value = "emulator-5554"
    with (
        patch("adapter.lifecycle.find_adb", return_value="adb"),
        patch("adapter.lifecycle.run_cmd", new=AsyncMock(return_value="device")),
    ):
        result = await lifecycle_action("state", {}, ctx)

    assert result.ok is True
    assert result.state == "running"


@pytest.mark.asyncio
async def test_unknown_action() -> None:
    result = await lifecycle_action("unknown", {}, _Ctx())
    assert result.ok is False
    assert "Unknown" in result.detail
