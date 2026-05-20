"""Reproducer for bug 5: ``_allocate_node_port`` probe ignores AGENT_GRID_NODE_BIND_HOST.

See ``docs/superpowers/specs/2026-05-20-agent-bug-audit.md`` (Bug 5).
"""

from __future__ import annotations

import socket as socket_module
from typing import TYPE_CHECKING

from agent_app.appium.process import AppiumProcessManager
from agent_app.config import agent_settings

if TYPE_CHECKING:
    import pytest


class _FakeSocket:
    """Minimal stand-in supporting the calls AppiumProcessManager._allocate_node_port makes."""

    def __init__(self, bind_calls: list[tuple[str, int]]) -> None:
        self._bind_calls = bind_calls

    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def setsockopt(self, *_: object, **__: object) -> None:
        return None

    def bind(self, address: tuple[str, int]) -> None:
        self._bind_calls.append(address)

    def close(self) -> None:
        return None


def test_node_port_probe_uses_configured_bind_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_settings.grid_node, "grid_node_bind_host", "127.0.0.1")

    bind_calls: list[tuple[str, int]] = []

    def fake_socket(family: int, type_: int) -> _FakeSocket:
        assert family == socket_module.AF_INET
        assert type_ == socket_module.SOCK_STREAM
        return _FakeSocket(bind_calls)

    monkeypatch.setattr("agent_app.appium.process.socket.socket", fake_socket)

    manager = AppiumProcessManager()
    manager._allocate_node_port()

    assert bind_calls
    assert bind_calls[0][0] == "127.0.0.1"
