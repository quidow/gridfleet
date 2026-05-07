"""Tests that PackStateLoop is wired into the agent lifespan correctly.

We use TestClient (which runs the lifespan) rather than ASGITransport (which
does not trigger lifespan events in httpx >= 0.23).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import unittest.mock

    import pytest

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from agent_app.main import app


def _mock_lifespan_deps() -> list[unittest.mock._patch[Any]]:
    """Return context managers that mock out network/subprocess calls in lifespan."""
    return [
        patch("agent_app.main.refresh_capabilities_snapshot", new_callable=AsyncMock),
        patch("agent_app.main.capabilities_refresh_loop", new_callable=AsyncMock),
        patch("agent_app.registration.register_with_manager", new_callable=AsyncMock, return_value=None),
    ]


def test_pack_state_loop_enabled_when_host_id_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_HOST_ID", "00000000-0000-0000-0000-000000000042")
    # Explicit backend URL so the loop task is created without a real network call.
    monkeypatch.setenv("AGENT_BACKEND_URL", "http://backend.invalid")

    mocks = _mock_lifespan_deps()
    for m in mocks:
        m.__enter__()
    try:
        with TestClient(app, raise_server_exceptions=True):
            assert app.state.pack_state_loop_enabled is True
    finally:
        for m in mocks:
            m.__exit__(None, None, None)


def test_pack_state_loop_disabled_without_host_id_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_HOST_ID", raising=False)
    monkeypatch.setenv("AGENT_BACKEND_URL", "http://backend.invalid")

    mocks = _mock_lifespan_deps()
    for m in mocks:
        m.__enter__()
    try:
        with TestClient(app, raise_server_exceptions=True):
            assert app.state.pack_state_loop_enabled is False
    finally:
        for m in mocks:
            m.__exit__(None, None, None)
