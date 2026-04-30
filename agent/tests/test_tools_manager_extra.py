import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from agent_app.tools_manager import (
    NodeProvider,
    _find_fnm_binary,
    _fnm_base_dirs,
    _fnm_default_bin_dirs,
    ensure_selenium_jar,
)


def test_node_provider_command_and_fnm_discovery_helpers() -> None:
    provider = NodeProvider(name="system", node_path="/usr/bin/node", npm_path="/usr/bin/npm")
    assert provider.command("node", "--version") == ["/usr/bin/node", "--version"]
    assert provider.command("npm", "install") == ["/usr/bin/npm", "install"]
    assert provider.command("appium", "--version") == ["appium", "--version"]

    with (
        patch("agent_app.tools_manager.shutil.which", return_value=None),
        patch("agent_app.tools_manager._is_executable", side_effect=lambda path: path == "/usr/local/bin/fnm"),
    ):
        assert _find_fnm_binary() == "/usr/local/bin/fnm"

    with patch.dict(os.environ, {"FNM_DIR": "~/custom-fnm", "XDG_DATA_HOME": "~/xdg"}, clear=True):
        bases = _fnm_base_dirs()
    assert os.path.expanduser("~/custom-fnm") in bases
    assert os.path.expanduser("~/xdg/fnm") in bases

    with patch("agent_app.tools_manager.os.path.isdir", side_effect=lambda path: path.endswith("/aliases/default/bin")):
        bins = _fnm_default_bin_dirs()
    assert all(path.endswith("/aliases/default/bin") for path in bins)


async def test_ensure_selenium_jar_covers_remaining_error_paths(tmp_path: Path) -> None:
    jar_path = tmp_path / "selenium-server.jar"

    assert await ensure_selenium_jar("   ", str(jar_path)) == {"success": True, "action": "skipped"}

    with patch("agent_app.tools_manager.get_selenium_jar_version", return_value="4.41.0"):
        assert await ensure_selenium_jar("4.41.0", str(jar_path)) == {
            "success": True,
            "action": "none",
            "version": "4.41.0",
            "path": str(jar_path),
        }

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.get.side_effect = httpx.HTTPError("download failed")
    with patch("agent_app.tools_manager.httpx.AsyncClient", return_value=client):
        result = await ensure_selenium_jar("4.41.0", str(jar_path))
    assert result["success"] is False
    assert "download failed" in result["error"]
