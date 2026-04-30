from pathlib import Path

import pytest

from agent_app.pack.runtime import AppiumRuntimeManager, RuntimeSpec


class PluginRunner:
    def __init__(self, fail_plugin: str | None = None) -> None:
        self.fail_plugin = fail_plugin
        self.plugin_calls: list[tuple[str, str, str, str | None, str]] = []

    async def install_appium(self, package: str, version: str, appium_home: str) -> str:
        return str(Path(appium_home) / "node_modules" / ".bin" / "appium")

    async def install_driver(
        self,
        name: str,
        package: str,
        version: str,
        appium_home: str,
        *,
        source: str = "npm",
        github_repo: str | None = None,
    ) -> None:
        return None

    async def install_plugin(
        self,
        name: str,
        version: str,
        source: str,
        package: str | None,
        appium_home: str,
    ) -> None:
        self.plugin_calls.append((name, version, source, package, appium_home))
        if name == self.fail_plugin:
            raise RuntimeError("peer dependency mismatch")


def _spec() -> RuntimeSpec:
    return RuntimeSpec(
        server_package="appium",
        server_version="2.11.5",
        drivers=(("appium-uiautomator2-driver", "3.6.0", "npm", None),),
        plugins=(("images", "1.0.0", "npm:appium-plugin-images", None),),
        node_major=None,
    )


@pytest.mark.asyncio
async def test_reconcile_installs_plugins_inside_runtime(tmp_path: Path) -> None:
    runner = PluginRunner()
    mgr = AppiumRuntimeManager(runner=runner, root_dir=tmp_path)

    envs, errors = await mgr.reconcile({"pack": _spec()})

    assert errors == {}
    env = envs["pack"]
    assert runner.plugin_calls == [("images", "1.0.0", "npm:appium-plugin-images", None, env.appium_home)]
    assert env.plugin_statuses == [
        {
            "name": "images",
            "version": "1.0.0",
            "source": "npm:appium-plugin-images",
            "package": None,
            "status": "installed",
            "blocked_reason": None,
        }
    ]


@pytest.mark.asyncio
async def test_plugin_failure_blocks_plugin_not_runtime(tmp_path: Path) -> None:
    runner = PluginRunner(fail_plugin="images")
    mgr = AppiumRuntimeManager(runner=runner, root_dir=tmp_path)

    envs, errors = await mgr.reconcile({"pack": _spec()})

    assert errors == {}
    assert envs["pack"].plugin_statuses[0]["status"] == "blocked"
    assert envs["pack"].plugin_statuses[0]["blocked_reason"] == "plugin_install_failed: peer dependency mismatch"
