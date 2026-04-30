from pathlib import Path

import pytest

from agent_app.pack.runtime import (
    AppiumRuntimeManager,
    RuntimeSpec,
    _driver_install_commands,
    _github_npm_install_spec,
)


class RecordingRunner:
    def __init__(self) -> None:
        self.appium_calls: list[tuple[str, str, str]] = []
        self.driver_calls: list[dict] = []

    async def install_appium(self, package: str, version: str, appium_home: str) -> str:
        self.appium_calls.append((package, version, appium_home))
        return f"{appium_home}/node_modules/.bin/appium"

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
        self.driver_calls.append(
            {
                "name": name,
                "package": package,
                "version": version,
                "source": source,
                "github_repo": github_repo,
            }
        )

    async def install_plugin(
        self,
        name: str,
        version: str,
        source: str,
        package: str | None,
        appium_home: str,
    ) -> None:
        return None


@pytest.mark.asyncio
async def test_github_source_passes_correct_args(tmp_path: Path) -> None:
    runner = RecordingRunner()
    mgr = AppiumRuntimeManager(runner=runner, root_dir=tmp_path)
    spec = RuntimeSpec(
        server_package="appium",
        server_version="2.11.5",
        drivers=(("roku", "0.13.3", "github", "dlenroc/appium-roku-driver"),),
        plugins=(),
        node_major=None,
    )
    await mgr.reconcile({"appium-roku-dlenroc": spec})
    assert len(runner.driver_calls) == 1
    assert runner.driver_calls[0]["source"] == "github"
    assert runner.driver_calls[0]["github_repo"] == "dlenroc/appium-roku-driver"


@pytest.mark.asyncio
async def test_npm_source_still_works(tmp_path: Path) -> None:
    runner = RecordingRunner()
    mgr = AppiumRuntimeManager(runner=runner, root_dir=tmp_path)
    spec = RuntimeSpec(
        server_package="appium",
        server_version="2.11.5",
        drivers=(("uiautomator2", "3.6.0", "npm", None),),
        plugins=(),
        node_major=None,
    )
    await mgr.reconcile({"appium-uiautomator2": spec})
    assert runner.driver_calls[0]["source"] == "npm"
    assert runner.driver_calls[0]["github_repo"] is None


def test_runtime_id_differs_by_source() -> None:
    spec_npm = RuntimeSpec(
        server_package="appium",
        server_version="2.11.5",
        drivers=(("roku", "0.13.3", "npm", None),),
        plugins=(),
        node_major=None,
    )
    spec_gh = RuntimeSpec(
        server_package="appium",
        server_version="2.11.5",
        drivers=(("roku", "0.13.3", "github", "dlenroc/appium-roku-driver"),),
        plugins=(),
        node_major=None,
    )
    assert AppiumRuntimeManager.runtime_id_for(spec_npm) != AppiumRuntimeManager.runtime_id_for(spec_gh)


def test_github_driver_installs_with_npm_git_url_then_appium_sync() -> None:
    commands = _driver_install_commands(
        "/runtimes/abc/node_modules/.bin/appium",
        "/runtimes/abc",
        "@dlenroc/appium-roku-driver",
        "0.13.1",
        "github",
        "dlenroc/appium-roku-driver",
    )

    assert commands == [
        [
            "npm",
            "install",
            "--prefix",
            "/runtimes/abc",
            "--save-dev",
            "--no-progress",
            "--no-audit",
            "git+https://github.com/dlenroc/appium-roku-driver.git#v0.13.1",
        ],
        ["/runtimes/abc/node_modules/.bin/appium", "driver", "list", "--installed"],
    ]


def test_github_install_spec_preserves_explicit_ref() -> None:
    assert (
        _github_npm_install_spec("dlenroc/appium-roku-driver#main", "0.13.1")
        == "git+https://github.com/dlenroc/appium-roku-driver.git#main"
    )
