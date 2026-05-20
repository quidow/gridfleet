"""Reproducer for bug 2: AGENT_GRID_NODE_SESSION_TIMEOUT_SEC is silently ignored.

See ``docs/superpowers/specs/2026-05-20-agent-bug-audit.md`` (Bug 2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from agent_app.appium import process as process_module
from agent_app.appium.process import AppiumLaunchSpec, AppiumProcessManager
from agent_app.config import agent_settings

if TYPE_CHECKING:
    import pytest


async def test_grid_node_session_timeout_sec_env_value_propagates_to_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_settings.grid_node, "grid_node_session_timeout_sec", 4242.0)

    manager = AppiumProcessManager()
    monkeypatch.setattr(manager, "_allocate_node_port", lambda: 5555)

    captured: dict[str, object] = {}

    def fake_start_supervisor(*, factory: object, config: object, clock: object | None = None) -> MagicMock:
        del factory, clock
        captured["config"] = config
        handle = MagicMock()
        handle.start = AsyncMock()
        handle.wait_until_running = AsyncMock()
        handle.stop = AsyncMock()
        handle.is_running.return_value = True
        handle.service = None
        return handle

    monkeypatch.setattr(process_module, "start_grid_node_supervisor", fake_start_supervisor)

    spec = AppiumLaunchSpec(
        connection_target="device-1",
        port=4723,
        plugins=None,
        extra_caps=None,
        stereotype_caps=None,
        session_override=False,
        device_type=None,
        ip_address=None,
        manage_grid_node=True,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )

    await manager._start_grid_node_service(spec)

    config = captured["config"]
    assert config.session_timeout_sec == 4242.0  # type: ignore[attr-defined]
