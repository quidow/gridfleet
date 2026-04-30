"""
Manual advanced example: connect to a Roku device, sideload a sample app, and take a screenshot.

Requires:
    - Selenium Grid hub running on localhost:4444
    - A Roku device registered with Roku dev credentials in device config
    - Appium with the Roku driver installed (`appium driver install roku`)
    - The supported GridFleet testkit installed
    - Appium-Python-Client installed (`uv pip install -e ./testkit[appium]`)

Run:
    cd testkit && python -m pytest examples/test_roku_sideload_screenshot.py -v -s
"""

from typing import Any

import pytest

from examples._example_helpers import (
    ROKU_HELLO_WORLD_APP,
    install_and_activate_roku_dev_app,
    print_connection_context,
    save_and_assert_screenshot,
)

pytest_plugins = ["gridfleet_testkit.pytest_plugin"]


@pytest.mark.parametrize(
    "appium_driver",
    [
        {
            "pack_id": "appium-roku-dlenroc",
            "platform_id": "roku_network",
        }
    ],
    indirect=True,
)
def test_roku_install_app_and_take_screenshot(appium_driver: Any) -> None:
    """Connect to a Roku device, sideload an app, and take a screenshot."""
    driver = appium_driver

    assert driver.session_id is not None, "Failed to create Appium session"

    print_connection_context(driver)

    install_and_activate_roku_dev_app(driver)
    print(f"App installed (sideloaded) successfully: {ROKU_HELLO_WORLD_APP}")
    print("App activated")

    save_and_assert_screenshot(driver, "roku_sideload")
