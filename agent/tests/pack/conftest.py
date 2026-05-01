from __future__ import annotations

import pytest

from agent_app.pack import tarball_fetch


@pytest.fixture(autouse=True)
def _clear_tarball_fetch_locks() -> None:
    tarball_fetch._fetch_locks.clear()
