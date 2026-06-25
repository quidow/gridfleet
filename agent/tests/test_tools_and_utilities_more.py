from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from agent_app.tools.manager import (
    CommandResult,
    NodeProvider,
    _detect_fnm_provider,
    _detect_nvm_provider,
    _detect_system_provider,
    _find_fnm_binary,
    _first_version,
    _fnm_base_dirs,
    _fnm_default_bin_dirs,
    _get_node_version,
    _is_executable,
    _prepend_process_path,
    _provider_env,
    _run_command,
    _run_optional,
    detect_node_provider,
)
from agent_app.tools.paths import _parse_node_version

if TYPE_CHECKING:
    import pytest


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
        patch("agent_app.tools.manager.build_env", return_value={"PATH": "/usr/bin"}),
        patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True),
    ):
        env = _provider_env(provider)
        _prepend_process_path(["/fnm/bin", "/usr/bin"])
        assert os.environ["PATH"] == "/fnm/bin:/usr/bin"

    assert env["PATH"] == "/fnm/bin:/usr/bin"


async def test_run_command_and_run_optional_cover_success_and_missing_binary() -> None:
    proc = _FakeProc(0, stdout=b"version")

    with patch("agent_app.tools.manager.asyncio.create_subprocess_exec", return_value=proc):
        result = await _run_command(["node", "--version"])

    assert result == CommandResult(0, "version")

    with patch("agent_app.tools.manager.asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        assert await _run_optional(["node", "--version"]) is None


def test_tools_manager_small_helpers() -> None:
    assert _first_version("node v20.11.1") == "20.11.1"


async def test_detect_fnm_provider_prefers_exec_and_has_error_fallback() -> None:
    with (
        patch("agent_app.tools.manager._find_fnm_binary", return_value="/usr/local/bin/fnm"),
        patch(
            "agent_app.tools.manager._run_optional",
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
        patch("agent_app.tools.manager._find_fnm_binary", return_value="/usr/local/bin/fnm"),
        patch("agent_app.tools.manager._run_optional", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools.manager._fnm_default_bin_dirs", return_value=[]),
    ):
        provider = await _detect_fnm_provider()

    assert provider == NodeProvider(name="fnm", node_path=None, npm_path=None, error="node_not_configured")


def test_detect_nvm_and_system_providers() -> None:
    with (
        patch(
            "agent_app.tools.manager.glob.glob",
            return_value=[
                "/Users/test/.nvm/versions/node/v18.0.0/bin/node",
                "/Users/test/.nvm/versions/node/v20.0.0/bin/node",
            ],
        ),
        patch(
            "agent_app.tools.manager._is_executable",
            side_effect=lambda path: path.endswith("/node") or path.endswith("/npm"),
        ),
    ):
        nvm = _detect_nvm_provider()

    assert nvm is not None
    assert nvm.node_path == "/Users/test/.nvm/versions/node/v20.0.0/bin/node"

    with (
        patch("agent_app.tools.manager.shutil.which", side_effect=[None, None]),
        patch(
            "agent_app.tools.manager._is_executable",
            side_effect=lambda path: path in {"/usr/local/bin/node", "/usr/local/bin/npm"},
        ),
    ):
        system = _detect_system_provider()

    assert system is not None
    assert system.npm_path == "/usr/local/bin/npm"


async def test_detect_node_provider_respects_precedence() -> None:
    provider = NodeProvider(name="fnm", node_path="/fnm/node", npm_path="/fnm/npm")

    with (
        patch("agent_app.tools.manager._detect_fnm_provider", new_callable=AsyncMock, return_value=provider),
        patch("agent_app.tools.manager._detect_nvm_provider") as nvm,
    ):
        assert await detect_node_provider() == provider

    nvm.assert_not_called()


async def test_get_node_version_handles_provider_command() -> None:
    provider = NodeProvider(
        name="fnm",
        node_path="/fnm/node",
        npm_path="/fnm/npm",
        command_prefix=["fnm", "exec", "--using", "default"],
    )

    with patch(
        "agent_app.tools.manager._run_optional",
        new_callable=AsyncMock,
        return_value=CommandResult(0, "v20.11.1"),
    ):
        assert await _get_node_version(provider) == "20.11.1"


# ---------------------------------------------------------------------------
# tool_paths coverage
# ---------------------------------------------------------------------------


def test_parse_node_version_invalid_returns_zero() -> None:
    with patch("agent_app.tools.paths.logger.debug") as mock_log:
        assert _parse_node_version("/foo/vABC.def/bin/appium") == (0,)
    mock_log.assert_called_once()


# ---------------------------------------------------------------------------
# tool_utils coverage
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# tools_manager small helpers coverage
# ---------------------------------------------------------------------------


def test_is_executable_false() -> None:
    with patch("agent_app.tools.manager.os.path.isfile", return_value=False):
        assert _is_executable("/some/path") is False


def test_find_fnm_binary_from_which() -> None:
    with patch("agent_app.tools.manager.shutil.which", return_value="/usr/bin/fnm"):
        assert _find_fnm_binary() == "/usr/bin/fnm"


def test_find_fnm_binary_fallback_found() -> None:
    with (
        patch("agent_app.tools.manager.shutil.which", return_value=None),
        patch(
            "agent_app.tools.manager._is_executable",
            side_effect=lambda p: p == "/opt/homebrew/bin/fnm",
        ),
    ):
        assert _find_fnm_binary() == "/opt/homebrew/bin/fnm"


def test_find_fnm_binary_not_found() -> None:
    with (
        patch("agent_app.tools.manager.shutil.which", return_value=None),
        patch("agent_app.tools.manager._is_executable", return_value=False),
    ):
        assert _find_fnm_binary() is None


def test_fnm_base_dirs_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FNM_DIR", os.path.expanduser("~/.local/share/fnm"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    dirs = _fnm_base_dirs()
    assert dirs.count(os.path.expanduser("~/.local/share/fnm")) == 1


def test_fnm_base_dirs_with_xdg_data_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FNM_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", "/xdg")
    dirs = _fnm_base_dirs()
    assert "/xdg/fnm" in dirs


def test_fnm_default_bin_dirs_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FNM_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    default_bin = os.path.expanduser("~/.local/share/fnm/aliases/default/bin")
    with patch(
        "agent_app.tools.manager.os.path.isdir",
        side_effect=lambda p: p == default_bin,
    ):
        bins = _fnm_default_bin_dirs()
        assert default_bin in bins


def test_node_provider_command_npm_and_default() -> None:
    provider = NodeProvider(name="nvm", node_path="/nvm/node", npm_path="/nvm/npm")
    assert provider.command("npm", "--version") == ["/nvm/npm", "--version"]
    provider2 = NodeProvider(name="system", node_path=None, npm_path=None)
    assert provider2.command("appium") == ["appium"]


async def test_detect_fnm_provider_none_when_fnm_not_found() -> None:
    with patch("agent_app.tools.manager._find_fnm_binary", return_value=None):
        assert await _detect_fnm_provider() is None
