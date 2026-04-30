import asyncio
from unittest.mock import AsyncMock, patch

from agent_app.plugin_manager import get_installed_plugins, install_plugin, sync_plugins


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


async def test_get_installed_plugins_parses_nested_payload() -> None:
    proc = _FakeProc(0, stdout=b'{"installed":{"execute-driver":{"version":"1.0.0","installed":true}}}')

    with (
        patch("agent_app.plugin_manager._find_appium", return_value="/usr/local/bin/appium"),
        patch("agent_app.plugin_manager._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.plugin_manager.asyncio.create_subprocess_exec", return_value=proc),
    ):
        plugins = await get_installed_plugins()

    assert plugins == [{"name": "execute-driver", "version": "1.0.0"}]


async def test_install_plugin_uses_npm_source() -> None:
    proc = _FakeProc(0, stdout=b"installed")

    with (
        patch("agent_app.plugin_manager.get_installed_plugins", new_callable=AsyncMock, return_value=[]),
        patch("agent_app.plugin_manager._find_appium", return_value="/usr/local/bin/appium"),
        patch("agent_app.plugin_manager._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.plugin_manager.asyncio.create_subprocess_exec", return_value=proc) as create_proc,
    ):
        result = await install_plugin("execute-driver", "1.0.0", "npm:@appium/execute-driver-plugin")

    assert result["success"] is True
    create_proc.assert_called_once()
    assert create_proc.call_args.args[:5] == (
        "/usr/local/bin/appium",
        "plugin",
        "install",
        "@appium/execute-driver-plugin@1.0.0",
        "--source=npm",
    )


async def test_sync_plugins_installs_updates_and_removes() -> None:
    with (
        patch(
            "agent_app.plugin_manager.get_installed_plugins",
            new_callable=AsyncMock,
            return_value=[
                {"name": "execute-driver", "version": "0.9.0"},
                {"name": "old-plugin", "version": "1.0.0"},
            ],
        ),
        patch(
            "agent_app.plugin_manager.install_plugin",
            new_callable=AsyncMock,
            return_value={"success": True, "message": "updated"},
        ) as install,
        patch(
            "agent_app.plugin_manager.uninstall_plugin",
            new_callable=AsyncMock,
            return_value={"success": True, "message": "removed"},
        ) as uninstall,
    ):
        result = await sync_plugins(
            [{"name": "execute-driver", "version": "1.0.0", "source": "npm:@appium/execute-driver-plugin"}]
        )

    install.assert_awaited_once_with("execute-driver", "1.0.0", "npm:@appium/execute-driver-plugin", None)
    uninstall.assert_awaited_once_with("old-plugin")
    assert result == {"installed": [], "updated": ["execute-driver"], "removed": ["old-plugin"], "errors": {}}
