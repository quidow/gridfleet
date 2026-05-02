from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from adapter.health import health_check


class _Ctx:
    def __init__(self, identity: str = "ABC123") -> None:
        self.device_identity_value = identity
        self.allow_boot = False


@pytest.mark.asyncio
async def test_health_check_uses_manifest_check_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        if cmd[-1] == "get-state":
            return "device"
        if cmd[-2:] == ["echo", "ok"]:
            return "ok"
        if cmd[-1] == "sys.boot_completed":
            return "1"
        return ""

    monkeypatch.setattr("adapter.health.run_cmd", fake_run_cmd)
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.health.tcp_reachable", AsyncMock(return_value=True))

    results = await health_check(_Ctx("192.168.1.254:5555"))

    assert [result.check_id for result in results] == [
        "adb_connected",
        "adb_responsive",
        "boot_completed",
        "ping",
    ]
    assert all(result.ok for result in results)


@pytest.mark.asyncio
async def test_health_check_resolves_avd_name_to_running_adb_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        commands.append(cmd)
        if cmd == ["adb", "devices"]:
            return "List of devices attached\nemulator-5554\tdevice\n"
        if cmd[:3] == ["adb", "-s", "emulator-5554"] and cmd[-1] == "get-state":
            return "device"
        if cmd[:3] == ["adb", "-s", "emulator-5554"] and cmd[-2:] == ["echo", "ok"]:
            return "ok"
        if cmd[:3] == ["adb", "-s", "emulator-5554"] and cmd[-1] == "sys.boot_completed":
            return "1"
        return ""

    monkeypatch.setattr("adapter.health.run_cmd", fake_run_cmd)
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.health.get_running_emulator_avd_name", AsyncMock(return_value="Pixel_6"))

    ctx = _Ctx("Pixel_6")
    ctx.device_type = "emulator"
    ctx.connection_type = "virtual"

    results = await health_check(ctx)

    assert all(result.ok for result in results)
    assert ["adb", "-s", "emulator-5554", "get-state"] in commands


@pytest.mark.asyncio
async def test_health_check_resolves_avd_name_without_adb_devices_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        commands.append(cmd)
        if cmd == ["adb", "devices"]:
            return ""
        if cmd[:3] == ["adb", "-s", "emulator-5554"] and cmd[-1] == "get-state":
            return "device"
        if cmd[:3] == ["adb", "-s", "emulator-5554"] and cmd[-2:] == ["echo", "ok"]:
            return "ok"
        if cmd[:3] == ["adb", "-s", "emulator-5554"] and cmd[-1] == "sys.boot_completed":
            return "1"
        return ""

    async def fake_avd_name(_adb: str, serial: str) -> str:
        return "Pixel_6" if serial == "emulator-5554" else ""

    monkeypatch.setattr("adapter.health.run_cmd", fake_run_cmd)
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.health.get_running_emulator_avd_name", fake_avd_name)

    ctx = _Ctx("Pixel_6")
    ctx.device_type = "emulator"
    ctx.connection_type = "virtual"

    results = await health_check(ctx)

    assert all(result.ok for result in results)
    assert ["adb", "-s", "emulator-5554", "get-state"] in commands


@pytest.mark.asyncio
@patch("adapter.health._adb_shell_echo", new_callable=AsyncMock, return_value=False)
async def test_unhealthy(_mock_echo: AsyncMock) -> None:
    results = await health_check(_Ctx())
    assert results[0].ok is False
