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
async def test_adb_unauthorized_true_when_device_listed_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        if cmd == ["adb", "devices"]:
            return "List of devices attached\n192.168.1.254:5555\tunauthorized\n"
        return ""

    monkeypatch.setattr("adapter.health.run_cmd", fake_run_cmd)
    from adapter.health import _adb_unauthorized

    assert await _adb_unauthorized("adb", "192.168.1.254:5555") is True


@pytest.mark.asyncio
async def test_adb_unauthorized_false_when_device(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        if cmd == ["adb", "devices"]:
            return "List of devices attached\n192.168.1.254:5555\tdevice\n"
        return ""

    monkeypatch.setattr("adapter.health.run_cmd", fake_run_cmd)
    from adapter.health import _adb_unauthorized

    assert await _adb_unauthorized("adb", "192.168.1.254:5555") is False


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


def _recommendation(results: list) -> str | None:
    for r in results:
        action = getattr(r, "recommended_action", None)
        if action:
            return action
    return None


@pytest.mark.asyncio
async def test_recommends_reconnect_when_link_dead_but_tcp_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        if cmd == ["adb", "devices"]:
            return "List of devices attached\n192.168.1.254:5555\toffline\n"
        return ""  # get-state, echo, boot_completed all fail (link dead)

    monkeypatch.setattr("adapter.health.run_cmd", fake_run_cmd)
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.health.tcp_reachable", AsyncMock(return_value=True))

    results = await health_check(_network_ctx(expected=None))

    assert _recommendation(results) == "reconnect"


@pytest.mark.asyncio
async def test_no_recommendation_when_tcp_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        if cmd == ["adb", "devices"]:
            return "List of devices attached\n192.168.1.254:5555\toffline\n"
        return ""

    monkeypatch.setattr("adapter.health.run_cmd", fake_run_cmd)
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.health.tcp_reachable", AsyncMock(return_value=False))

    results = await health_check(_network_ctx(expected=None))

    assert _recommendation(results) is None


@pytest.mark.asyncio
async def test_unauthorized_no_recommendation_and_emits_check(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        if cmd == ["adb", "devices"]:
            return "List of devices attached\n192.168.1.254:5555\tunauthorized\n"
        return ""

    monkeypatch.setattr("adapter.health.run_cmd", fake_run_cmd)
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.health.tcp_reachable", AsyncMock(return_value=True))

    results = await health_check(_network_ctx(expected=None))

    assert _recommendation(results) is None
    assert any(r.check_id == "adb_unauthorized" and not r.ok for r in results)


@pytest.mark.asyncio
async def test_usb_never_recommends(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        return ""  # link dead

    monkeypatch.setattr("adapter.health.run_cmd", fake_run_cmd)
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")
    results = await health_check(_Ctx("ABC123"))  # usb-style serial, connection_type None

    assert _recommendation(results) is None


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


def _network_ctx(expected: str | None = "G070EXPECTED") -> _Ctx:
    ctx = _Ctx("192.168.1.254:5555")
    ctx.connection_type = "network"
    if expected is not None:
        ctx.expected_identity_value = expected  # type: ignore[attr-defined]
    return ctx


def _fake_run_cmd_factory(serialno: str | None) -> object:
    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        if cmd[-1] == "get-state":
            return "device"
        if cmd[-2:] == ["echo", "ok"]:
            return "ok"
        if cmd[-1] == "sys.boot_completed":
            return "1"
        if cmd[-1] == "ro.serialno":
            if serialno is None:
                raise TimeoutError("adb hung")
            return serialno
        return ""

    return fake_run_cmd


@pytest.mark.asyncio
async def test_health_check_identity_mismatch_fails_for_network_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different device answering at the stored adb target is a definitive failure."""
    monkeypatch.setattr("adapter.health.run_cmd", _fake_run_cmd_factory("G070STRANGER"))
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.health.tcp_reachable", AsyncMock(return_value=True))

    results = await health_check(_network_ctx())

    identity = next(result for result in results if result.check_id == "identity")
    assert identity.ok is False
    assert "G070STRANGER" in identity.detail


@pytest.mark.asyncio
async def test_health_check_identity_match_passes_for_network_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("adapter.health.run_cmd", _fake_run_cmd_factory("G070EXPECTED"))
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.health.tcp_reachable", AsyncMock(return_value=True))

    results = await health_check(_network_ctx())

    identity = next(result for result in results if result.check_id == "identity")
    assert identity.ok is True


@pytest.mark.asyncio
async def test_health_check_identity_inconclusive_serial_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty/failed serial read must not flap health — the check is omitted."""
    monkeypatch.setattr("adapter.health.run_cmd", _fake_run_cmd_factory(""))
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.health.tcp_reachable", AsyncMock(return_value=True))

    results = await health_check(_network_ctx())

    assert not any(result.check_id == "identity" for result in results)


@pytest.mark.asyncio
async def test_health_check_identity_skipped_for_usb_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """USB devices are addressed by serial — nothing to verify."""

    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        assert cmd[-1] != "ro.serialno", "identity must not be queried for usb devices"
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

    ctx = _Ctx("ABC123")
    ctx.connection_type = "usb"
    ctx.expected_identity_value = "ABC123"  # type: ignore[attr-defined]
    results = await health_check(ctx)

    assert not any(result.check_id == "identity" for result in results)


@pytest.mark.asyncio
async def test_health_check_identity_skipped_when_not_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An adb-disconnected target cannot answer a serial query — skip it."""

    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        assert cmd[-1] != "ro.serialno", "identity must not be queried when disconnected"
        return ""

    monkeypatch.setattr("adapter.health.run_cmd", fake_run_cmd)
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.health.tcp_reachable", AsyncMock(return_value=False))

    results = await health_check(_network_ctx())

    assert not any(result.check_id == "identity" for result in results)


def _orphan_ctx(*, has_live: bool | None, ports: dict[str, int] | None) -> _Ctx:
    ctx = _Ctx()  # plain USB serial — no ping check interference
    ctx.has_live_session = has_live  # type: ignore[attr-defined]
    ctx.claimed_ports = ports  # type: ignore[attr-defined]
    return ctx


@pytest.mark.asyncio
async def test_orphan_system_port_detected_when_no_live_session(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        if cmd[-1] == "get-state":
            return "device"
        if cmd[-1] == "ok":
            return "ok"
        if cmd[-1] == "sys.boot_completed":
            return "1"
        return ""

    monkeypatch.setattr("adapter.health.run_cmd", fake_run_cmd)
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.health.tcp_reachable", AsyncMock(return_value=True))  # port BOUND

    results = await health_check(_orphan_ctx(has_live=False, ports={"appium:systemPort": 8200}))

    failed = [r for r in results if r.check_id == "claimed_ports_free"]
    assert len(failed) == 1 and failed[0].ok is False
    assert "8200" in failed[0].detail
    assert _recommendation(results) == "release_forwarded_ports"


@pytest.mark.asyncio
async def test_orphan_check_green_when_port_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("adapter.health._adb_shell_echo", AsyncMock(return_value=True))
    monkeypatch.setattr("adapter.health.run_cmd", AsyncMock(return_value="device"))
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.health.tcp_reachable", AsyncMock(return_value=False))  # port free

    results = await health_check(_orphan_ctx(has_live=False, ports={"appium:systemPort": 8200}))

    entry = [r for r in results if r.check_id == "claimed_ports_free"]
    assert len(entry) == 1 and entry[0].ok is True and entry[0].recommended_action is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("has_live", "ports"),
    [
        (True, {"appium:systemPort": 8200}),  # session live → port legitimately bound
        (None, {"appium:systemPort": 8200}),  # unknown (old backend) → degrade to skip
        (False, None),  # no claims supplied → skip
        (False, {"appium:chromedriverPort": 9515}),  # no systemPort claim → skip
    ],
)
async def test_orphan_check_skipped(
    monkeypatch: pytest.MonkeyPatch, has_live: bool | None, ports: dict[str, int] | None
) -> None:
    monkeypatch.setattr("adapter.health.run_cmd", AsyncMock(return_value="device"))
    monkeypatch.setattr("adapter.health.find_adb", lambda: "adb")
    connect = AsyncMock(return_value=True)
    monkeypatch.setattr("adapter.health.tcp_reachable", connect)

    results = await health_check(_orphan_ctx(has_live=has_live, ports=ports))

    assert not [r for r in results if r.check_id == "claimed_ports_free"]
