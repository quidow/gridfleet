"""
Manual baseline example: connect to a tvOS real device through Selenium Grid and take a screenshot.

Requires:
    - Selenium Grid hub running on localhost:4444
    - A tvOS real device registered and its Appium node running
    - The supported GridFleet testkit installed
    - Appium with the XCUITest driver installed (`appium driver install xcuitest`)
    - tvOS real-device prerequisites already configured on the host, including WebDriverAgent/XCUITest setup
    - Appium-Python-Client installed (`uv pip install -e ./testkit[appium]`)

Run:
    cd testkit && python -m pytest examples/test_tvos_screenshot.py -v -s
"""

from typing import Any

import pytest

from examples._example_helpers import print_connection_context, save_and_assert_screenshot

pytest_plugins = ["gridfleet_testkit.pytest_plugin"]


@pytest.mark.parametrize(
    "appium_driver",
    [
        {
            "pack_id": "appium-xcuitest",
            "platform_id": "tvos",
        }
    ],
    indirect=True,
)
def test_tvos_take_screenshot(appium_driver: Any) -> None:
    """Connect to a tvOS real device through the Grid and take a screenshot."""
    driver = appium_driver

    assert driver.session_id is not None, "Failed to create Appium session"

    print_connection_context(driver)
    save_and_assert_screenshot(driver, "tvos")
