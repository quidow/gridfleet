"""Reproducer for bug 10: ``AppiumProcessManager.start`` unconditionally
discards ``_stop_pending_ports`` (process.py:856-857). When the auto-restart
loop calls ``start()`` after a crash, any stop_pending intent set by the
operator during the restart window is silently lost — start() also never
schedules an idle-stop task for ``stop_pending=True``.

See ``.superpowers/specs/2026-05-20-agent-bug-audit.md`` (Bug 10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from agent_app.appium.process import AppiumProcessManager
from agent_app.pack.runtime_registry import RuntimeRegistry

if TYPE_CHECKING:
    import pytest


async def test_start_with_stop_pending_preserves_stop_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = AppiumProcessManager()
    manager.set_runtime_registry(RuntimeRegistry())

    fake_proc = MagicMock()
    fake_proc.returncode = None
    fake_proc.pid = 12345

    async def fake_start_appium_server(spec: object, *, clear_logs_on_failure: bool) -> MagicMock:
        del spec, clear_logs_on_failure
        return fake_proc

    monkeypatch.setattr(manager, "_start_appium_server", fake_start_appium_server)
    monkeypatch.setattr(manager, "_start_grid_node_service", AsyncMock())

    await manager.start(
        connection_target="dev-1",
        platform_id="android_mobile",
        port=4723,
        grid_url="http://hub:4444",
        pack_id="pack-1",
        stop_pending=True,
    )

    assert 4723 in manager._stop_pending_ports
