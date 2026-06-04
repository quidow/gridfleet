from pathlib import Path

import pytest

from agent_app.config import agent_settings


@pytest.fixture(autouse=True)
def isolated_runtime_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep per-port appium log files out of the real AGENT_RUNTIME_ROOT.

    Spawn, stop, get_logs, and lifespan's log-maintenance sweep all touch
    ``<runtime_root>/appium-logs``. On a host with the production agent
    installed, an unisolated sweep would delete real log files (cf. the
    installer-test home-dir isolation in ``tests/installer/conftest.py``).
    """
    monkeypatch.setattr(agent_settings.runtime, "runtime_root", str(tmp_path))
