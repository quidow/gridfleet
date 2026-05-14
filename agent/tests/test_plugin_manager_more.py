import asyncio
from unittest.mock import AsyncMock, patch

from agent_app.plugins.manager import (
    _install_command,
    _versioned,
    get_installed_plugins,
    install_plugin,
    uninstall_plugin,
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


def test_versioned_leaves_existing_suffix_untouched() -> None:
    assert _versioned("@scope/plugin@1.0.0", "2.0.0") == "@scope/plugin@1.0.0"


def test_install_command_supports_all_sources() -> None:
    assert _install_command("appium", "images", "1.2.3", "images", None) == [
        "appium",
        "plugin",
        "install",
        "images@1.2.3",
    ]
    assert _install_command("appium", "images", "1.2.3", "npm:@appium/images-plugin", None) == [
        "appium",
        "plugin",
        "install",
        "@appium/images-plugin@1.2.3",
        "--source=npm",
    ]
    assert _install_command("appium", "images", "1.2.3", "github:org/repo", "@appium/images-plugin") == [
        "appium",
        "plugin",
        "install",
        "org/repo",
        "--source=github",
        "--package=@appium/images-plugin",
    ]
    assert _install_command("appium", "images", "1.2.3", "git:https://example.com/repo.git", "pkg") == [
        "appium",
        "plugin",
        "install",
        "https://example.com/repo.git",
        "--source=git",
        "--package=pkg",
    ]
    assert _install_command("appium", "images", "1.2.3", "local:/tmp/plugin", "pkg") == [
        "appium",
        "plugin",
        "install",
        "/tmp/plugin",
        "--source=local",
        "--package=pkg",
    ]


async def test_get_installed_plugins_returns_empty_on_nonzero_status() -> None:
    with (
        patch("agent_app.plugins.manager._find_appium", return_value="/usr/local/bin/appium"),
        patch("agent_app.plugins.manager._build_env", return_value={"PATH": "/usr/bin"}),
        patch(
            "agent_app.plugins.manager.asyncio.create_subprocess_exec",
            return_value=_FakeProc(1, stderr=b"boom"),
        ),
    ):
        plugins = await get_installed_plugins()

    assert plugins == []


async def test_get_installed_plugins_returns_empty_on_invalid_json() -> None:
    with (
        patch("agent_app.plugins.manager._find_appium", return_value="/usr/local/bin/appium"),
        patch("agent_app.plugins.manager._build_env", return_value={"PATH": "/usr/bin"}),
        patch(
            "agent_app.plugins.manager.asyncio.create_subprocess_exec",
            return_value=_FakeProc(0, stdout=b"not-json"),
        ),
    ):
        plugins = await get_installed_plugins()

    assert plugins == []


async def test_get_installed_plugins_skips_uninstalled_entries() -> None:
    proc = _FakeProc(
        0,
        stdout=b'{"images":{"version":"1.0.0","installed":true},"relaxed-caps":{"version":"2.0.0","installed":false}}',
    )

    with (
        patch("agent_app.plugins.manager._find_appium", return_value="/usr/local/bin/appium"),
        patch("agent_app.plugins.manager._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.plugins.manager.asyncio.create_subprocess_exec", return_value=proc),
    ):
        plugins = await get_installed_plugins()

    assert plugins == [{"name": "images", "version": "1.0.0"}]


async def test_install_plugin_noops_when_requested_version_is_present() -> None:
    with (
        patch(
            "agent_app.plugins.manager.get_installed_plugins",
            new_callable=AsyncMock,
            return_value=[{"name": "images", "version": "1.0.0"}],
        ),
        patch("agent_app.plugins.manager.asyncio.create_subprocess_exec", new_callable=AsyncMock) as create_proc,
    ):
        result = await install_plugin("images", "1.0.0", "images")

    assert result == {"success": True, "message": "images@1.0.0 already installed"}
    create_proc.assert_not_called()


async def test_install_plugin_uninstalls_previous_version_before_reinstalling() -> None:
    proc = _FakeProc(0, stdout=b"installed")

    with (
        patch(
            "agent_app.plugins.manager.get_installed_plugins",
            new_callable=AsyncMock,
            return_value=[{"name": "images", "version": "0.9.0"}],
        ),
        patch("agent_app.plugins.manager.uninstall_plugin", new_callable=AsyncMock) as uninstall,
        patch("agent_app.plugins.manager._find_appium", return_value="/usr/local/bin/appium"),
        patch("agent_app.plugins.manager._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.plugins.manager.asyncio.create_subprocess_exec", return_value=proc) as create_proc,
    ):
        result = await install_plugin("images", "1.0.0", "images")

    uninstall.assert_awaited_once_with("images")
    assert create_proc.call_args.args[:4] == ("/usr/local/bin/appium", "plugin", "install", "images@1.0.0")
    assert result == {"success": True, "message": "installed"}


async def test_install_plugin_surfaces_missing_binary_and_timeout() -> None:
    with (
        patch("agent_app.plugins.manager.get_installed_plugins", new_callable=AsyncMock, return_value=[]),
        patch("agent_app.plugins.manager._find_appium", return_value="/usr/local/bin/appium"),
        patch("agent_app.plugins.manager._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.plugins.manager.asyncio.create_subprocess_exec", side_effect=FileNotFoundError),
    ):
        missing = await install_plugin("images", "1.0.0", "images")

    with (
        patch("agent_app.plugins.manager.get_installed_plugins", new_callable=AsyncMock, return_value=[]),
        patch("agent_app.plugins.manager._find_appium", return_value="/usr/local/bin/appium"),
        patch("agent_app.plugins.manager._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.plugins.manager.asyncio.create_subprocess_exec", return_value=_FakeProc(0)),
        patch("agent_app.plugins.manager.asyncio.wait_for", side_effect=TimeoutError),
    ):
        timed_out = await install_plugin("images", "1.0.0", "images")

    assert missing == {"success": False, "error": "appium binary not found"}
    assert timed_out == {"success": False, "error": "install timed out after 120s"}


async def test_install_plugin_returns_stderr_when_install_fails() -> None:
    with (
        patch("agent_app.plugins.manager.get_installed_plugins", new_callable=AsyncMock, return_value=[]),
        patch("agent_app.plugins.manager._find_appium", return_value="/usr/local/bin/appium"),
        patch("agent_app.plugins.manager._build_env", return_value={"PATH": "/usr/bin"}),
        patch(
            "agent_app.plugins.manager.asyncio.create_subprocess_exec",
            return_value=_FakeProc(1, stdout=b"stdout", stderr=b"stderr"),
        ),
    ):
        result = await install_plugin("images", "1.0.0", "images")

    assert result == {"success": False, "error": "stdoutstderr"}


async def test_uninstall_plugin_handles_error_paths() -> None:
    with (
        patch("agent_app.plugins.manager._find_appium", return_value="/usr/local/bin/appium"),
        patch("agent_app.plugins.manager._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.plugins.manager.asyncio.create_subprocess_exec", side_effect=FileNotFoundError),
    ):
        missing = await uninstall_plugin("images")

    with (
        patch("agent_app.plugins.manager._find_appium", return_value="/usr/local/bin/appium"),
        patch("agent_app.plugins.manager._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.plugins.manager.asyncio.create_subprocess_exec", return_value=_FakeProc(0)),
        patch("agent_app.plugins.manager.asyncio.wait_for", side_effect=TimeoutError),
    ):
        timed_out = await uninstall_plugin("images")

    with (
        patch("agent_app.plugins.manager._find_appium", return_value="/usr/local/bin/appium"),
        patch("agent_app.plugins.manager._build_env", return_value={"PATH": "/usr/bin"}),
        patch(
            "agent_app.plugins.manager.asyncio.create_subprocess_exec",
            return_value=_FakeProc(1, stdout=b"", stderr=b"nope"),
        ),
    ):
        failed = await uninstall_plugin("images")

    assert missing == {"success": False, "error": "appium binary not found"}
    assert timed_out == {"success": False, "error": "uninstall timed out"}
    assert failed == {"success": False, "error": "nope"}
