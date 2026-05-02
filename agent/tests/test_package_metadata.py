from __future__ import annotations

from importlib.metadata import entry_points, version

import agent_app


def test_public_version_matches_installed_distribution() -> None:
    assert agent_app.__version__ == version("gridfleet-agent")


def test_console_script_entry_point_is_declared() -> None:
    console_scripts = entry_points(group="console_scripts")

    assert any(script.name == "gridfleet-agent" and script.value == "agent_app.cli:main" for script in console_scripts)
