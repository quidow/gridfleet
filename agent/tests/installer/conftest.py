"""Shared isolation for installer tests.

The installer resolves real user paths (``Path.home()``, ``$HOME``) and the
tests exercise ``install``/``uninstall``/``status`` flows with explicit
``os_name`` values regardless of the host OS. Without isolation, a test run
on a macOS host deletes the *production* launchd plist at
``~/Library/LaunchAgents/com.gridfleet.agent.plist`` (observed on a lab
host: the bootstrapped service kept running from launchd's in-memory copy
while the file silently vanished). Force every installer test into a
throwaway home so no test can read or write the operator's real files;
individual tests may still re-patch ``Path.home`` on top of this.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))  # type: ignore[arg-type]
    monkeypatch.setenv("HOME", str(home))
    return home
