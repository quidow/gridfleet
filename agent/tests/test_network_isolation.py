"""The guard that keeps the guard.

Running this suite must never reach a real backend. A single unmocked
``TestClient`` lifespan once registered the developer's machine as a host and
fenced the live agent out for five minutes; the fixture these tests pin is what
makes that class of leak harmless.
"""

from __future__ import annotations

import os

from agent_app.config import ManagerSettings, agent_settings
from tests.conftest import UNROUTABLE_MANAGER_URL


def test_config_singleton_is_pinned_to_the_sentinel() -> None:
    assert agent_settings.manager.manager_url == UNROUTABLE_MANAGER_URL
    assert agent_settings.manager.effective_backend_url == UNROUTABLE_MANAGER_URL


def test_environment_pins_the_sentinel_for_rebuilt_settings() -> None:
    """``importlib.reload`` and bare ``ManagerSettings()`` read the environment."""
    assert os.environ["AGENT_MANAGER_URL"] == UNROUTABLE_MANAGER_URL
    assert "AGENT_BACKEND_URL" not in os.environ
    assert ManagerSettings().effective_backend_url == UNROUTABLE_MANAGER_URL
