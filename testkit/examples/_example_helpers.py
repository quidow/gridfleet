"""Shared helpers for copyable manual screenshot examples."""

from __future__ import annotations

from pathlib import Path
from typing import Any

SCREENSHOT_DIR = Path(__file__).parent / "screenshots"
ROKU_HELLO_WORLD_APP = Path(__file__).parent / "assets" / "hello-world.zip"


def _resolved_connection_target(capabilities: dict[str, Any]) -> str:
    value = capabilities.get("appium:udid")
    if isinstance(value, str) and value:
        return value
    session_id = capabilities.get("sessionId")
    if isinstance(session_id, str) and session_id:
        return session_id
    return "session"


def print_connection_context(driver: Any) -> str:
    """Print the resolved session context and return the connection target string."""
    caps = driver.capabilities
    connection_target = _resolved_connection_target(caps)
    platform_name = caps.get("platformName", "")
    automation_name = caps.get("appium:automationName") or caps.get("automationName")
    print(
        "\nConnected to device: "
        f"connection_target={connection_target}, "
        f"platform={platform_name}, "
        f"automationName={automation_name}"
    )
    print(f"Session ID: {driver.session_id}")
    return connection_target


def save_and_assert_screenshot(driver: Any, example_name: str) -> Path:
    """Save a screenshot and assert that the written file is non-empty."""
    caps = driver.capabilities
    connection_target = _resolved_connection_target(caps).replace(":", "_")
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    screenshot_path = SCREENSHOT_DIR / f"{example_name}_{connection_target}.png"
    saved = driver.save_screenshot(str(screenshot_path))

    assert saved, "save_screenshot returned False"
    assert screenshot_path.exists(), f"Screenshot file not found at {screenshot_path}"
    assert screenshot_path.stat().st_size > 0, "Screenshot file is empty"

    print(f"Screenshot saved: {screenshot_path} ({screenshot_path.stat().st_size} bytes)")
    return screenshot_path


def install_and_activate_roku_dev_app(driver: Any) -> None:
    """Install and activate the bundled Roku dev app used by screenshot examples."""
    assert ROKU_HELLO_WORLD_APP.exists(), f"App package not found: {ROKU_HELLO_WORLD_APP}"
    driver.install_app(str(ROKU_HELLO_WORLD_APP.resolve()))
    driver.activate_app("dev")
