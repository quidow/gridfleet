"""
Manual baseline example: connect to an Android mobile device through Selenium Grid and take a screenshot.

Requires:
    - Selenium Grid hub running on localhost:4444
    - An Android mobile device registered and its Appium node running
    - The supported GridFleet testkit installed
    - Appium-Python-Client installed (`uv pip install -e ./testkit`)

Run:
    cd testkit && python -m pytest examples/test_android_mobile_screenshot.py -v -s
"""

import pytest
from appium.webdriver.webdriver import WebDriver

from examples._example_helpers import print_connection_context, save_and_assert_screenshot

pytest_plugins = ["gridfleet_testkit.pytest_plugin"]


@pytest.mark.parametrize(
    "appium_driver",
    [
        {
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
        }
    ],
    indirect=True,
)
def test_android_mobile_take_screenshot(appium_driver: WebDriver) -> None:
    """Connect to an Android mobile device through the Grid and take a screenshot."""
    driver = appium_driver

    assert driver.session_id is not None, "Failed to create Appium session"

    print_connection_context(driver)
    save_and_assert_screenshot(driver, "android_mobile")
