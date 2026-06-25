from typing import TYPE_CHECKING

import pytest

from agent_app import http_client
from agent_app.config import agent_settings

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
async def reset_shared_http_client() -> object:
    """Close the process-wide httpx client after each test on its own event loop.

    The client is cached module-globally and only recreated when closed, so a
    pooled connection created on one test's loop would otherwise be reused on the
    next, raising "Event loop is closed" when httpcore tears it down (surfaced on
    Python 3.14's stricter loop lifecycle).
    """
    yield
    await http_client.close()


@pytest.fixture(autouse=True)
def isolated_runtime_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep per-port appium log files out of the real AGENT_RUNTIME_ROOT.

    Spawn, stop, get_logs, and lifespan's log-maintenance sweep all touch
    ``<runtime_root>/appium-logs``. On a host with the production agent
    installed, an unisolated sweep would delete real log files (cf. the
    installer-test home-dir isolation in ``tests/installer/conftest.py``).
    """
    monkeypatch.setattr(agent_settings.runtime, "runtime_root", str(tmp_path))
