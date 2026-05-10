from unittest.mock import AsyncMock, patch

from agent_app.tools_manager import (
    CommandResult,
    NodeProvider,
    ensure_appium,
    get_tool_status,
)


async def test_ensure_appium_noops_when_version_matches() -> None:
    provider = NodeProvider(name="fnm", node_path="/fnm/bin/node", npm_path="/fnm/bin/npm", bin_paths=["/fnm/bin"])

    with (
        patch("agent_app.tools_manager.detect_node_provider", new_callable=AsyncMock, return_value=provider),
        patch("agent_app.tools_manager._get_appium_version", new_callable=AsyncMock, return_value="3.3.0"),
        patch("agent_app.tools_manager._run_optional", new_callable=AsyncMock) as run,
    ):
        result = await ensure_appium("3.3.0")

    assert result == {"success": True, "action": "none", "version": "3.3.0", "node_provider": "fnm"}
    run.assert_not_awaited()


async def test_ensure_appium_installs_with_provider_npm() -> None:
    provider = NodeProvider(
        name="fnm",
        node_path="/fnm/bin/node",
        npm_path="/fnm/bin/npm",
        bin_paths=["/fnm/bin"],
        command_prefix=["/usr/local/bin/fnm", "exec", "--using", "default"],
    )

    with (
        patch("agent_app.tools_manager.detect_node_provider", new_callable=AsyncMock, return_value=provider),
        patch(
            "agent_app.tools_manager._get_appium_version",
            new_callable=AsyncMock,
            side_effect=["3.2.0", "3.3.0"],
        ),
        patch(
            "agent_app.tools_manager._run_optional",
            new_callable=AsyncMock,
            return_value=CommandResult(0, "installed"),
        ) as run,
        patch("agent_app.tools_manager.refresh_capabilities_snapshot", new_callable=AsyncMock) as refresh,
    ):
        result = await ensure_appium("3.3.0")

    run.assert_awaited_once()
    assert run.await_args.args[0] == [
        "/usr/local/bin/fnm",
        "exec",
        "--using",
        "default",
        "npm",
        "install",
        "-g",
        "appium@3.3.0",
    ]
    refresh.assert_awaited_once()
    assert result["success"] is True
    assert result["action"] == "updated"
    assert result["node_provider"] == "fnm"


async def test_ensure_appium_reports_missing_node() -> None:
    with (
        patch("agent_app.tools_manager.detect_node_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._get_appium_version", new_callable=AsyncMock, return_value=None),
    ):
        result = await ensure_appium("3.3.0")

    assert result == {"success": False, "error": "node_not_found"}


async def test_ensure_appium_reports_fnm_without_configured_node() -> None:
    provider = NodeProvider(name="fnm", node_path=None, npm_path=None, error="node_not_configured")
    with (
        patch("agent_app.tools_manager.detect_node_provider", new_callable=AsyncMock, return_value=provider),
        patch("agent_app.tools_manager._get_appium_version", new_callable=AsyncMock, return_value=None),
    ):
        result = await ensure_appium("3.3.0")

    assert result == {"success": False, "error": "node_not_configured", "node_provider": "fnm"}


async def test_get_tool_status_returns_nulls_for_absent_tools() -> None:
    with (
        patch("agent_app.tools_manager.detect_node_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._get_node_version", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._get_appium_version", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._get_go_ios_version", new_callable=AsyncMock, return_value=None),
    ):
        status = await get_tool_status()

    assert status["appium"] is None
    assert status["node"] is None
    assert status["node_provider"] is None
    assert status["go_ios"] is None
    assert "selenium_jar" not in status
    assert "selenium_jar_path" not in status


async def test_get_tool_status_includes_go_ios_version() -> None:
    with (
        patch("agent_app.tools_manager.detect_node_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._get_node_version", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._get_appium_version", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._get_go_ios_version", new_callable=AsyncMock, return_value="1.0.207"),
    ):
        status = await get_tool_status()

    assert status["go_ios"] == "1.0.207"
