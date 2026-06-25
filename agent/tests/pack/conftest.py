from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_app.pack import adapter_loader, discovery, tarball_fetch


@pytest.fixture(autouse=True)
def _clear_tarball_fetch_locks() -> None:
    tarball_fetch._fetch_locks.clear()


@pytest.fixture(autouse=True)
def _clear_discovery_sweep_cache() -> None:
    discovery._sweep_cache.clear()


@pytest.fixture(autouse=True)
def _clear_adapter_cache() -> None:
    """Reset the process-global adapter cache and prune stale ``sys.path`` entries.

    Tests create runtime directories under ``tmp_path`` that disappear between
    cases; leaving their ``site/`` entries on ``sys.path`` causes ``importlib``
    to resolve a stale ``adapter`` module on the next load.
    """
    adapter_loader._cache.clear()
    adapter_loader._cache_install_locks.clear()
    sys.path[:] = [entry for entry in sys.path if not entry or Path(entry).exists()]
    adapter_loader._drop_adapter_modules()
