from __future__ import annotations

import sys

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
    """Reset the process-global adapter cache and drop loaded adapter modules.

    Adapters import under unique ``gridfleet_adapter_*`` module names backed by
    runtime directories under ``tmp_path`` that disappear between cases.
    """
    adapter_loader._cache.clear()
    adapter_loader._cache_install_locks.clear()
    for name in list(sys.modules):
        if name.startswith("gridfleet_adapter_"):
            sys.modules.pop(name, None)
