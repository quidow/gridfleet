from unittest.mock import AsyncMock, patch

from agent_app.tools_manager import (
    get_tool_status,
)


async def test_get_tool_status_returns_nulls_for_absent_tools() -> None:
    with (
        patch("agent_app.tools_manager.detect_node_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._get_node_version", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._get_go_ios_version", new_callable=AsyncMock, return_value=None),
    ):
        status = await get_tool_status()

    assert status["node"] is None
    assert status["node_provider"] is None
    assert status["go_ios"] is None
    assert "appium" not in status
    assert "selenium_jar" not in status
    assert "selenium_jar_path" not in status


async def test_get_tool_status_includes_go_ios_version() -> None:
    with (
        patch("agent_app.tools_manager.detect_node_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._get_node_version", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._get_go_ios_version", new_callable=AsyncMock, return_value="1.0.207"),
    ):
        status = await get_tool_status()

    assert status["go_ios"] == "1.0.207"
