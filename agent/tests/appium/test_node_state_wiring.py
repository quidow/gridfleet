from __future__ import annotations

from contextlib import AbstractContextManager, ExitStack
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from agent_app.config import RuntimeSettings, agent_settings
from agent_app.host.capabilities import CapabilitiesCache
from agent_app.main import app
from agent_app.registration import RegistrationService

if TYPE_CHECKING:
    import pytest


def _mock_lifespan_deps() -> list[AbstractContextManager[object]]:
    return [
        patch.object(CapabilitiesCache, "refresh", new_callable=AsyncMock),
        patch.object(CapabilitiesCache, "run_refresh_loop", new_callable=AsyncMock),
        patch.object(RegistrationService, "register_once", new_callable=AsyncMock, return_value=None),
    ]


def test_node_pull_settings_are_enabled_by_default() -> None:
    settings = RuntimeSettings()

    assert settings.node_pull_enabled is True
    assert settings.node_poll_interval_sec == 5.0


def test_node_state_loop_is_not_constructed_when_flag_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_settings.runtime, "node_pull_enabled", False)
    monkeypatch.setattr(agent_settings.core, "host_id", "00000000-0000-0000-0000-000000000042")

    with ExitStack() as stack:
        for mock in _mock_lifespan_deps():
            stack.enter_context(mock)
        loop_cls = stack.enter_context(patch("agent_app.lifespan.NodeStateLoop"))
        with TestClient(app, raise_server_exceptions=True):
            assert app.state.node_state_loop is None
            loop_cls.assert_not_called()


def test_node_state_loop_starts_when_flag_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_settings.runtime, "node_pull_enabled", True)
    monkeypatch.setattr(agent_settings.runtime, "node_poll_interval_sec", 1.25)
    monkeypatch.setattr(agent_settings.core, "host_id", "00000000-0000-0000-0000-000000000042")

    with ExitStack() as stack:
        for mock in _mock_lifespan_deps():
            stack.enter_context(mock)
        loop_cls = stack.enter_context(patch("agent_app.lifespan.NodeStateLoop"))
        loop = loop_cls.return_value
        loop.run_forever = AsyncMock()
        with TestClient(app, raise_server_exceptions=True):
            assert app.state.node_state_loop is loop
            assert loop_cls.call_args.kwargs["poll_interval"] == 1.25
