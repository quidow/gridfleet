from __future__ import annotations

from typing import Any
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
    assert result.state == "running"
    assert result.resolved_connection_target == "emulator-5554"
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


class _LCtx:
    host_id = "h"
    device_identity_value = "192.168.1.50:5555"


def _ladder_args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "ip_address": "192.168.1.50",
        "claimed_ports": {"appium:systemPort": 8200},
        "has_live_session": False,
        "host_has_live_sessions": False,
    }
    args.update(overrides)
    return args


def _bound_sequence(*states: bool) -> AsyncMock:
    """tcp_reachable fake: each call pops the next bound-state."""
    return AsyncMock(side_effect=list(states))


@pytest.mark.asyncio
async def test_ladder_rung1_forward_remove_cures(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        commands.append(cmd)
        return ""

    monkeypatch.setattr("adapter.lifecycle.run_cmd", fake_run_cmd)
    monkeypatch.setattr("adapter.lifecycle.find_adb", lambda: "adb")
    # initial probe: bound; after rung 1: free
    monkeypatch.setattr("adapter.lifecycle.tcp_reachable", _bound_sequence(True, False))

    result = await lifecycle_action("release_forwarded_ports", _ladder_args(), _LCtx())

    assert result.ok is True
    assert "cured_by=forward_remove" in result.detail
    assert ["adb", "-s", "192.168.1.50:5555", "forward", "--remove", "tcp:8200"] in commands
    assert not any("kill-server" in c for c in commands)


@pytest.mark.asyncio
async def test_ladder_rung2_rebind_cures(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        commands.append(cmd)
        return ""

    monkeypatch.setattr("adapter.lifecycle.run_cmd", fake_run_cmd)
    monkeypatch.setattr("adapter.lifecycle.find_adb", lambda: "adb")
    # initial: bound; after rung 1: still bound; after rung 2: free
    monkeypatch.setattr("adapter.lifecycle.tcp_reachable", _bound_sequence(True, True, False))

    result = await lifecycle_action("release_forwarded_ports", _ladder_args(), _LCtx())

    assert result.ok is True
    assert "cured_by=rebind_remove" in result.detail
    assert ["adb", "-s", "192.168.1.50:5555", "forward", "tcp:8200", "tcp:6790"] in commands


@pytest.mark.asyncio
async def test_ladder_bounce_cures_and_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        commands.append(cmd)
        return ""

    monkeypatch.setattr("adapter.lifecycle.run_cmd", fake_run_cmd)
    monkeypatch.setattr("adapter.lifecycle.find_adb", lambda: "adb")
    # initial, after R1, after R2: bound; after bounce: free
    monkeypatch.setattr("adapter.lifecycle.tcp_reachable", _bound_sequence(True, True, True, False))

    result = await lifecycle_action("release_forwarded_ports", _ladder_args(), _LCtx())

    assert result.ok is True
    assert "cured_by=adb_bounce" in result.detail
    assert ["adb", "kill-server"] in commands
    assert ["adb", "connect", "192.168.1.50:5555"] in commands


@pytest.mark.asyncio
async def test_ladder_bounce_blocked_by_host_live_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    async def fake_run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
        commands.append(cmd)
        return ""

    monkeypatch.setattr("adapter.lifecycle.run_cmd", fake_run_cmd)
    monkeypatch.setattr("adapter.lifecycle.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.lifecycle.tcp_reachable", _bound_sequence(True, True, True))

    result = await lifecycle_action(
        "release_forwarded_ports", _ladder_args(host_has_live_sessions=True), _LCtx()
    )

    assert result.ok is False
    assert "bounce blocked" in result.detail
    assert not any("kill-server" in c for c in commands)


@pytest.mark.asyncio
async def test_ladder_refuses_when_session_appeared() -> None:
    result = await lifecycle_action("release_forwarded_ports", _ladder_args(has_live_session=True), _LCtx())
    assert result.ok is False and "refused" in result.detail


@pytest.mark.asyncio
async def test_ladder_noop_when_port_already_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("adapter.lifecycle.find_adb", lambda: "adb")
    monkeypatch.setattr("adapter.lifecycle.tcp_reachable", _bound_sequence(False))

    result = await lifecycle_action("release_forwarded_ports", _ladder_args(), _LCtx())

    assert result.ok is True and "cured_by=none" in result.detail
