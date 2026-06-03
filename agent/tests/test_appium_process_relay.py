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
