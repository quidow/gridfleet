"""Verify AppiumProcessManager exposes public grid-supervisor accessors."""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_app.appium.process import AppiumProcessManager


def test_iter_grid_supervisors_returns_items() -> None:
    manager = AppiumProcessManager()
    supervisor_a = MagicMock(name="supervisor_a")
    supervisor_b = MagicMock(name="supervisor_b")
    manager._grid_supervisors[4444] = supervisor_a
    manager._grid_supervisors[4445] = supervisor_b

    pairs = list(manager.iter_grid_supervisors())
    assert (4444, supervisor_a) in pairs
    assert (4445, supervisor_b) in pairs
    assert len(pairs) == 2


def test_pop_grid_supervisor_removes_entry() -> None:
    manager = AppiumProcessManager()
    supervisor = MagicMock(name="supervisor")
    manager._grid_supervisors[5555] = supervisor

    manager.pop_grid_supervisor(5555)
    assert 5555 not in manager._grid_supervisors


def test_pop_grid_supervisor_missing_port_noop() -> None:
    manager = AppiumProcessManager()
    manager.pop_grid_supervisor(9999)
