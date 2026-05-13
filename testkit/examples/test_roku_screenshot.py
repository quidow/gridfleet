"""
Manual baseline example: connect to a Roku device through Selenium Grid and take a screenshot.

Requires:
    - Selenium Grid hub running on localhost:4444
    - A Roku device registered with Roku dev credentials in device config
    - Appium with the Roku driver installed (`appium driver install roku`)
    - The supported GridFleet testkit installed
    - Appium-Python-Client installed (`uv pip install -e ./testkit`)

Run:
    cd testkit && python -m pytest examples/test_roku_screenshot.py -v -s
"""

import pytest
from appium.webdriver.webdriver import WebDriver

from examples._example_helpers import (
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
def test_roku_take_screenshot(appium_driver: WebDriver) -> None:
    """Connect to a Roku device through the Grid and take a screenshot."""
    driver = appium_driver

    assert driver.session_id is not None, "Failed to create Appium session"

    print_connection_context(driver)
    install_and_activate_roku_dev_app(driver)
    save_and_assert_screenshot(driver, "roku")
