from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from adapter.health import health_check


class _Ctx:
    def __init__(self, identity: str = "ABC123") -> None:
        self.device_identity_value = identity
        self.allow_boot = False
        self.platform_id: str | None = None
        self.device_type: str | None = None
        self.connection_type: str | None = None
        self.ip_address: str | None = None
        self.ip_ping_timeout_sec: float | None = None
        self.ip_ping_count: int | None = None


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


# ---------------------------------------------------------------------------
# ip_ping health check tests
# ---------------------------------------------------------------------------


def _make_adb_fake() -> tuple[list[list[str]], object]:
    """Return a (commands, fake_run_cmd) pair for standard ADB fakes."""
    commands: list[list[str]] = []

    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        commands.append(cmd)
        if cmd[-1] == "get-state":
            return "device"
        if cmd[-2:] == ["echo", "ok"]:
            return "ok"
        if cmd[-1] == "sys.boot_completed":
            return "1"
        return ""

    return commands, fake_run_cmd


@pytest.mark.asyncio
async def test_health_check_emits_ip_ping_when_ip_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _commands, fake_run_cmd = _make_adb_fake()
    monkeypatch.setattr("adapter.health.run_cmd", fake_run_cmd)
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.health.icmp_reachable", AsyncMock(return_value=True))

    ctx = _Ctx("ABC123")
    ctx.connection_type = "usb"
    ctx.ip_address = "10.0.0.7"

    results = await health_check(ctx)

    ip_ping_results = [r for r in results if r.check_id == "ip_ping"]
    assert len(ip_ping_results) == 1
    assert ip_ping_results[0].ok is True
    assert ip_ping_results[0].detail == ""


@pytest.mark.asyncio
async def test_health_check_omits_ip_ping_when_ip_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _commands, fake_run_cmd = _make_adb_fake()
    monkeypatch.setattr("adapter.health.run_cmd", fake_run_cmd)
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")

    icmp_called = []

    async def recording_icmp_reachable(host: str, *, timeout: float = 2.0, count: int = 1) -> bool:
        icmp_called.append(host)
        return True

    monkeypatch.setattr("adapter.health.icmp_reachable", recording_icmp_reachable)

    ctx = _Ctx("ABC123")
    ctx.connection_type = "usb"
    ctx.ip_address = None

    results = await health_check(ctx)

    assert not any(r.check_id == "ip_ping" for r in results)
    assert icmp_called == [], "icmp_reachable must not be called when ip_address is None"


@pytest.mark.asyncio
async def test_health_check_omits_ip_ping_when_connection_type_not_usb(monkeypatch: pytest.MonkeyPatch) -> None:
    _commands, fake_run_cmd = _make_adb_fake()
    monkeypatch.setattr("adapter.health.run_cmd", fake_run_cmd)
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.health.get_running_emulator_avd_name", AsyncMock(return_value="Pixel_6"))

    icmp_called = []

    async def recording_icmp_reachable(host: str, *, timeout: float = 2.0, count: int = 1) -> bool:
        icmp_called.append(host)
        return True

    monkeypatch.setattr("adapter.health.icmp_reachable", recording_icmp_reachable)

    ctx = _Ctx("Pixel_6")
    ctx.connection_type = "virtual"
    ctx.device_type = "emulator"
    ctx.ip_address = "10.0.0.7"

    results = await health_check(ctx)

    assert not any(r.check_id == "ip_ping" for r in results)
    assert icmp_called == [], "icmp_reachable must not be called when connection_type != 'usb'"


@pytest.mark.asyncio
async def test_health_check_marks_ip_ping_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _commands, fake_run_cmd = _make_adb_fake()
    monkeypatch.setattr("adapter.health.run_cmd", fake_run_cmd)
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.health.icmp_reachable", AsyncMock(return_value=False))

    ctx = _Ctx("ABC123")
    ctx.connection_type = "usb"
    ctx.ip_address = "10.0.0.7"

    results = await health_check(ctx)

    ip_ping_results = [r for r in results if r.check_id == "ip_ping"]
    assert len(ip_ping_results) == 1
    assert ip_ping_results[0].ok is False
    assert ip_ping_results[0].detail != "", "detail must be non-empty on failure"
