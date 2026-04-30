from __future__ import annotations

from typing import Any
from urllib.parse import quote

import pytest


@pytest.mark.e2e_hardware
@pytest.mark.parametrize(
    "appium_driver",
    [
        {
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
            "appium:automationName": "UiAutomator2",
            "appium:appPackage": "io.appium.android.apis",
            "appium:appActivity": ".ApiDemos",
        }
    ],
    indirect=True,
)
def test_android_native_app_launches(appium_driver: Any) -> None:
    driver = appium_driver

    assert driver.session_id is not None
    assert driver.current_package == "io.appium.android.apis"
    assert "API Demos" in driver.page_source


@pytest.mark.e2e_hardware
@pytest.mark.parametrize(
    "appium_driver",
    [
        {
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
            "appium:automationName": "UiAutomator2",
            "browserName": "Chrome",
        }
    ],
    indirect=True,
)
def test_android_chrome_session_renders_page(appium_driver: Any) -> None:
    driver = appium_driver
    page_title = "GridFleet Android E2E"
    page_body = "Chrome lane is healthy."
    html = f"<html><head><title>{page_title}</title></head><body>{page_body}</body></html>"
    data_url = f"data:text/html;charset=utf-8,{quote(html)}"

    driver.get(data_url)

    assert driver.session_id is not None
    assert driver.title == page_title
    assert page_body in driver.page_source
