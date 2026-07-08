from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(autouse=True)
def _stub_node_poke(monkeypatch: pytest.MonkeyPatch) -> Generator[AsyncMock]:
    """Run creation/cooldown/node-control paths poke the device's agent inline
    via ``poke_node_refresh`` -> ``agent_nodes_refresh``. Stub it so tests don't
    pay a real network call/timeout; a test can request this fixture by name to
    assert the poke was awaited. A test's own more specific monkeypatch on
    ``agent_nodes_refresh`` (applied later, in the test body) overrides this.
    """
    mock = AsyncMock()
    monkeypatch.setattr("app.agent_comm.operations.agent_nodes_refresh", mock)
    yield mock
