import asyncio
import os
from unittest.mock import AsyncMock, patch

from agent_app.tools_manager import (
    CommandResult,
    NodeProvider,
    _detect_fnm_provider,
    _detect_nvm_provider,
    _detect_system_provider,
    _first_version,
    _get_appium_version,
    _get_node_version,
    _prepend_process_path,
    _provider_env,
    _run_command,
    _run_optional,
    detect_node_provider,
    ensure_appium,
    ensure_tools,
)


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def communicate(self) -> asyncio.Future[tuple[bytes, bytes]]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[bytes, bytes]] = loop.create_future()
        future.set_result((self._stdout, self._stderr))
        return future


def test_provider_env_and_prepend_process_path_manage_unique_paths() -> None:
    provider = NodeProvider(name="fnm", node_path="/fnm/bin/node", npm_path="/fnm/bin/npm", bin_paths=["/fnm/bin"])

    with (
        patch("agent_app.tools_manager._build_env", return_value={"PATH": "/usr/bin"}),
        patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True),
    ):
        env = _provider_env(provider)
        _prepend_process_path(["/fnm/bin", "/usr/bin"])
        assert os.environ["PATH"] == "/fnm/bin:/usr/bin"

    assert env["PATH"] == "/fnm/bin:/usr/bin"


async def test_run_command_and_run_optional_cover_success_and_missing_binary() -> None:
    proc = _FakeProc(0, stdout=b"version")

    with patch("agent_app.tools_manager.asyncio.create_subprocess_exec", return_value=proc):
        result = await _run_command(["node", "--version"])

    assert result == CommandResult(0, "version")

    with patch("agent_app.tools_manager.asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        assert await _run_optional(["node", "--version"]) is None


def test_tools_manager_small_helpers() -> None:
    assert _first_version("node v20.11.1") == "20.11.1"


async def test_detect_fnm_provider_prefers_exec_and_has_error_fallback() -> None:
    with (
        patch("agent_app.tools_manager._find_fnm_binary", return_value="/usr/local/bin/fnm"),
        patch(
            "agent_app.tools_manager._run_optional",
            new_callable=AsyncMock,
            side_effect=[
                CommandResult(0, "/fnm/versions/node"),
                CommandResult(0, "/fnm/versions/npm"),
                CommandResult(0, "v20.11.1"),
            ],
        ),
    ):
        provider = await _detect_fnm_provider()

    assert provider == NodeProvider(
        name="fnm",
        node_path="/fnm/versions/node",
        npm_path="/fnm/versions/npm",
        bin_paths=["/fnm/versions"],
        command_prefix=["/usr/local/bin/fnm", "exec", "--using", "default"],
    )

    with (
        patch("agent_app.tools_manager._find_fnm_binary", return_value="/usr/local/bin/fnm"),
        patch("agent_app.tools_manager._run_optional", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools_manager._fnm_default_bin_dirs", return_value=[]),
    ):
        provider = await _detect_fnm_provider()

    assert provider == NodeProvider(name="fnm", node_path=None, npm_path=None, error="node_not_configured")


def test_detect_nvm_and_system_providers() -> None:
    with (
        patch(
            "agent_app.tools_manager.glob.glob",
            return_value=[
                "/Users/test/.nvm/versions/node/v18.0.0/bin/node",
                "/Users/test/.nvm/versions/node/v20.0.0/bin/node",
            ],
        ),
        patch(
            "agent_app.tools_manager._is_executable",
            side_effect=lambda path: path.endswith("/node") or path.endswith("/npm"),
        ),
    ):
        nvm = _detect_nvm_provider()

    assert nvm is not None
    assert nvm.node_path == "/Users/test/.nvm/versions/node/v20.0.0/bin/node"

    with (
        patch("agent_app.tools_manager.shutil.which", side_effect=[None, None]),
        patch(
            "agent_app.tools_manager._is_executable",
            side_effect=lambda path: path in {"/usr/local/bin/node", "/usr/local/bin/npm"},
        ),
    ):
        system = _detect_system_provider()

    assert system is not None
    assert system.npm_path == "/usr/local/bin/npm"


async def test_detect_node_provider_respects_precedence() -> None:
    provider = NodeProvider(name="fnm", node_path="/fnm/node", npm_path="/fnm/npm")

    with (
        patch("agent_app.tools_manager._detect_fnm_provider", new_callable=AsyncMock, return_value=provider),
        patch("agent_app.tools_manager._detect_nvm_provider") as nvm,
    ):
        assert await detect_node_provider() == provider

    nvm.assert_not_called()


async def test_get_node_and_appium_versions_handle_fallbacks() -> None:
    provider = NodeProvider(
        name="fnm",
        node_path="/fnm/node",
        npm_path="/fnm/npm",
        command_prefix=["fnm", "exec", "--using", "default"],
    )

    with patch(
        "agent_app.tools_manager._run_optional",
        new_callable=AsyncMock,
        return_value=CommandResult(0, "v20.11.1"),
    ):
        assert await _get_node_version(provider) == "20.11.1"

    with patch(
        "agent_app.tools_manager._run_optional",
        new_callable=AsyncMock,
        side_effect=[CommandResult(1, "bad"), CommandResult(0, "3.1.0")],
    ):
        assert await _get_appium_version(provider) == "3.1.0"


async def test_ensure_appium_and_other_tool_flows_cover_remaining_branches() -> None:
    assert await ensure_appium("   ") == {"success": True, "action": "skipped"}

    provider = NodeProvider(name="system", node_path="/usr/bin/node", npm_path=None, command_prefix=[])
    with (
        patch("agent_app.tools_manager.detect_node_provider", new_callable=AsyncMock, return_value=provider),
        patch("agent_app.tools_manager._get_appium_version", new_callable=AsyncMock, return_value=None),
    ):
        assert await ensure_appium("3.3.0") == {"success": False, "error": "node_not_found", "node_provider": "system"}

    provider = NodeProvider(name="fnm", node_path="/fnm/node", npm_path="/fnm/npm", command_prefix=["fnm"])
    with (
        patch("agent_app.tools_manager.detect_node_provider", new_callable=AsyncMock, return_value=provider),
        patch("agent_app.tools_manager._get_appium_version", new_callable=AsyncMock, side_effect=[None, "3.2.0"]),
        patch(
            "agent_app.tools_manager._run_optional",
            new_callable=AsyncMock,
            return_value=CommandResult(0, "installed"),
        ),
    ):
        mismatch = await ensure_appium("3.3.0")

    assert mismatch["error"] == "installed_version_mismatch"

    with patch("agent_app.tools_manager.ensure_appium", new_callable=AsyncMock, return_value={"success": True}):
        combined = await ensure_tools("3.3.0")

    assert combined == {"appium": {"success": True}}
