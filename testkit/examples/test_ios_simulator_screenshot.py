"""
Manual baseline example: connect to an iOS simulator through Selenium Grid and take a screenshot.

Requires:
    - Selenium Grid hub running on localhost:4444
    - An iOS simulator registered and its Appium node running
    - The supported GridFleet testkit installed
    - Appium with the XCUITest driver installed (`appium driver install xcuitest`)
    - Appium-Python-Client installed (`uv pip install -e ./testkit`)

Run:
    cd testkit && python -m pytest examples/test_ios_simulator_screenshot.py -v -s
"""

import pytest
from appium.webdriver.webdriver import WebDriver

from examples._example_helpers import print_connection_context, save_and_assert_screenshot


@pytest.mark.parametrize(
    "appium_driver",
    [
        {
            "pack_id": "appium-xcuitest",
            "platform_id": "ios",
            "appium:device_type": "simulator",
        }
    ],
    indirect=True,
)
def test_ios_take_screenshot(appium_driver: WebDriver) -> None:
    """Connect to an iOS simulator through the Grid and take a screenshot."""
    driver = appium_driver

    assert driver.session_id is not None, "Failed to create Appium session"

    print_connection_context(driver)
    save_and_assert_screenshot(driver, "ios")
