"""PluginService must forward the agent BasicAuth pool to its backend→agent calls.

Without the pool the request is unauthenticated and the agent rejects it when the
auth gate is enabled, so host plugin fetch/sync silently fails.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.plugins.service import PluginService
from tests.fakes import FakeSettingsReader

pytestmark = pytest.mark.asyncio

POOL = Mock()


async def test_fetch_host_plugins_forwards_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr("app.plugins.service.list_agent_plugins", mock)
    svc = PluginService(settings=FakeSettingsReader(), circuit_breaker=Mock(), pool=POOL)

    await svc.fetch_host_plugins(SimpleNamespace(ip="127.0.0.1", agent_port=5100))

    assert mock.await_args.kwargs["pool"] is POOL


async def test_sync_host_plugins_forwards_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = AsyncMock(return_value={})
    monkeypatch.setattr("app.plugins.service.sync_agent_plugins", mock)
    svc = PluginService(settings=FakeSettingsReader(), circuit_breaker=Mock(), pool=POOL)

    await svc.sync_host_plugins(SimpleNamespace(ip="127.0.0.1", agent_port=5100), [])

    assert mock.await_args.kwargs["pool"] is POOL
