import io
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_app.tools_manager import (
    CommandResult,
    NodeProvider,
    ensure_appium,
    ensure_selenium_jar,
    get_selenium_jar_version,
    get_tool_status,
)


def _jar_bytes(version: str, *, key: str = "Selenium-Version") -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as jar:
        jar.writestr(
            "META-INF/MANIFEST.MF",
            f"Manifest-Version: 1.0\n{key}: {version}\n",
        )
    return data.getvalue()


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


def test_get_selenium_jar_version_reads_selenium_version(tmp_path: Path) -> None:
    jar_path = tmp_path / "selenium-server.jar"
    jar_path.write_bytes(_jar_bytes("4.41.0"))

    assert get_selenium_jar_version(str(jar_path)) == "4.41.0"


def test_get_selenium_jar_version_falls_back_to_implementation_version(tmp_path: Path) -> None:
    jar_path = tmp_path / "selenium-server.jar"
    jar_path.write_bytes(_jar_bytes("4.40.0", key="Implementation-Version"))

    assert get_selenium_jar_version(str(jar_path)) == "4.40.0"


async def test_ensure_selenium_jar_downloads_mismatched_version(tmp_path: Path) -> None:
    jar_path = tmp_path / "selenium-server.jar"
    jar_path.write_bytes(_jar_bytes("4.40.0"))

    response = MagicMock()
    response.content = _jar_bytes("4.41.0")
    response.raise_for_status.return_value = None

    client = MagicMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.get = AsyncMock(return_value=response)

    with (
        patch("agent_app.tools_manager.httpx.AsyncClient", return_value=client),
        patch("agent_app.tools_manager.refresh_capabilities_snapshot", new_callable=AsyncMock) as refresh,
    ):
        result = await ensure_selenium_jar("4.41.0", str(jar_path))

    assert result["success"] is True
    assert result["action"] == "updated"
    assert get_selenium_jar_version(str(jar_path)) == "4.41.0"
    refresh.assert_awaited_once()


async def test_get_tool_status_returns_nulls_for_absent_tools() -> None:
    with (
        patch("agent_app.tools_manager.detect_node_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._get_node_version", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._get_appium_version", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._get_go_ios_version", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager.get_selenium_jar_version", return_value=None),
    ):
        status = await get_tool_status()

    assert status["appium"] is None
    assert status["node"] is None
    assert status["node_provider"] is None
    assert status["go_ios"] is None
    assert status["selenium_jar"] is None


async def test_get_tool_status_includes_go_ios_version() -> None:
    with (
        patch("agent_app.tools_manager.detect_node_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._get_node_version", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._get_appium_version", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._get_go_ios_version", new_callable=AsyncMock, return_value="1.0.207"),
        patch("agent_app.tools_manager.get_selenium_jar_version", return_value=None),
    ):
        status = await get_tool_status()

    assert status["go_ios"] == "1.0.207"


@pytest.mark.asyncio
async def test_ensure_selenium_jar_rejects_malformed_version_without_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jar_path = tmp_path / "selenium-server.jar"

    async def fail_if_http_client_created(*args: object, **kwargs: object) -> object:
        raise AssertionError("malformed Selenium version must not reach httpx")

    monkeypatch.setattr("agent_app.tools_manager.httpx.AsyncClient", fail_if_http_client_created)

    result = await ensure_selenium_jar("4.41.0/../../latest", str(jar_path))

    assert result == {"success": False, "error": "invalid_selenium_version", "version": None}
    assert not jar_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["1!4.41.0", "4.41.0.post1"])
async def test_ensure_selenium_jar_rejects_epoch_and_postrelease_versions_without_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
) -> None:
    jar_path = tmp_path / "selenium-server.jar"

    async def fail_if_http_client_created(*args: object, **kwargs: object) -> object:
        raise AssertionError("non-canonical Selenium version must not reach httpx")

    monkeypatch.setattr("agent_app.tools_manager.httpx.AsyncClient", fail_if_http_client_created)

    result = await ensure_selenium_jar(version, str(jar_path))

    assert result == {"success": False, "error": "invalid_selenium_version", "version": None}
    assert not jar_path.exists()
