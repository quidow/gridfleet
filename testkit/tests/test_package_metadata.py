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


def test_bucket_a_public_exports_are_available() -> None:
    expected = {
        "AllocatedDevice",
        "build_error_session_payload",
        "hydrate_allocated_device",
        "hydrate_allocated_device_from_driver",
    }

    assert expected.issubset(set(gridfleet_testkit.__all__))
    for name in expected:
        assert getattr(gridfleet_testkit, name) is not None


def test_documented_public_exports_are_available() -> None:
    expected = {
        "AllocatedDevice",
        "GridFleetClient",
        "HeartbeatThread",
        "ReserveCapabilitiesUnsupportedError",
        "UnavailableInclude",
        "UnknownIncludeError",
        "build_appium_options",
        "build_error_session_payload",
        "create_appium_driver",
        "get_connection_target_from_driver",
        "get_device_config_for_driver",
        "get_device_test_data_for_driver",
        "hydrate_allocated_device",
        "hydrate_allocated_device_from_driver",
        "register_run_cleanup",
    }

    assert expected.issubset(set(gridfleet_testkit.__all__))
    for name in expected:
        assert getattr(gridfleet_testkit, name) is not None
