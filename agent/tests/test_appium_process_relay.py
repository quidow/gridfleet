from __future__ import annotations

from typing import TYPE_CHECKING

from agent_app.appium.process import AppiumProcessManager
from agent_app.config import agent_settings

if TYPE_CHECKING:
    import pytest


def test_allocate_control_port_starts_at_configured_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_settings.grid_node, "relay_control_port_start", 7900)
    manager = AppiumProcessManager()
    first = manager._allocate_control_port()
    second = manager._allocate_control_port()
    assert first >= 7900
    assert second > first


def test_allocate_control_port_skips_bound_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    monkeypatch.setattr(agent_settings.grid_node, "relay_control_port_start", 7950)
    manager = AppiumProcessManager()
    with socket.socket() as blocker:
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 7950))
        blocker.listen(1)
        port = manager._allocate_control_port()
    assert port != 7950


def test_allocate_node_port_skips_loopback_shadowed_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    # A wildcard probe bind succeeds even when a loopback-only listener (an
    # Android emulator's adb console on 127.0.0.1, canonically port 5555)
    # holds the port — but that listener then shadows the node on the
    # loopback path the fast-lane sidecar's admin endpoints depend on. The
    # allocator must skip such ports.
    import socket

    monkeypatch.setattr(agent_settings.grid_node, "grid_node_bind_host", "0.0.0.0")
    with socket.socket() as loopback_squatter:
        loopback_squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        loopback_squatter.bind(("127.0.0.1", 0))
        loopback_squatter.listen(1)
        squatted_port = int(loopback_squatter.getsockname()[1])

        monkeypatch.setattr(agent_settings.grid_node, "grid_node_port_start", squatted_port)
        manager = AppiumProcessManager()
        allocated = manager._allocate_node_port()
        assert allocated != squatted_port
