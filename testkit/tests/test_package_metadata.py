from __future__ import annotations

from importlib.metadata import entry_points, version

import gridfleet_testkit


def test_public_version_matches_installed_distribution() -> None:
    assert gridfleet_testkit.__version__ == version("gridfleet-testkit")


def test_pytest_plugin_entry_point_is_declared() -> None:
    pytest_plugins = entry_points(group="pytest11")

    assert any(
        plugin.name == "gridfleet" and plugin.value == "gridfleet_testkit.pytest_plugin" for plugin in pytest_plugins
    )
