import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_app.pack.runtime import (
    AppiumRuntimeManager,
    NpmRunner,
    RealNpmRunner,
    RuntimeSpec,
    _driver_install_commands,
    _github_npm_install_spec,
    _github_ref,
    _plugin_install_command,
    _versioned,
)


class _FakeRunner(NpmRunner):
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def install_appium(self, package: str, version: str, appium_home: str) -> str:
        self.calls.append(("install_appium", package, version, appium_home))
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
        self.calls.append(("install_driver", name, package, version, appium_home))

    async def install_plugin(
        self,
        name: str,
        version: str,
        source: str,
        package: str | None,
        appium_home: str,
    ) -> None:
        self.calls.append(("install_plugin", name, version, source, package or "", appium_home))


class _AlreadyInstalledRunner(_FakeRunner):
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
        self.calls.append(("install_driver", name, package, version, appium_home))
        raise RuntimeError('Error: A driver named "uiautomator2" is already installed.')


class _DriverInstallMutatesAppiumRunner(_FakeRunner):
    async def install_appium(self, package: str, version: str, appium_home: str) -> str:
        self.calls.append(("install_appium", package, version, appium_home))
        package_dir = Path(appium_home) / "node_modules" / package
        package_dir.mkdir(parents=True)
        (package_dir / "package.json").write_text(json.dumps({"version": version}))
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
        self.calls.append(("install_driver", name, package, version, appium_home))
        appium_package = Path(appium_home) / "node_modules" / "appium" / "package.json"
        appium_package.write_text(json.dumps({"version": "2.19.0"}))
        driver_package = Path(appium_home) / "node_modules" / package / "package.json"
        driver_package.parent.mkdir(parents=True)
        driver_package.write_text(json.dumps({"version": "4.2.0"}))


def _spec(
    server: str = "2.11.5",
    driver_name: str = "appium-uiautomator2-driver",
    driver_version: str = "3.6.0",
) -> RuntimeSpec:
    return RuntimeSpec(
        server_package="appium",
        server_version=server,
        drivers=((driver_name, driver_version, "npm", None),),
        plugins=(),
        node_major=None,
    )


def test_runtime_id_is_stable() -> None:
    assert AppiumRuntimeManager.runtime_id_for(_spec()) == AppiumRuntimeManager.runtime_id_for(_spec())


def test_runtime_id_differs_for_different_server_versions() -> None:
    spec1 = AppiumRuntimeManager.runtime_id_for(_spec(server="2.11.5"))
    spec2 = AppiumRuntimeManager.runtime_id_for(_spec(server="2.2.2"))
    assert spec1 != spec2


@pytest.mark.asyncio
async def test_reconcile_installs_each_runtime_once(tmp_path: object) -> None:
    runner = _FakeRunner()
    mgr = AppiumRuntimeManager(runner=runner, root_dir=tmp_path)  # type: ignore
    envs, _errors = await mgr.reconcile({"appium-uiautomator2": _spec()})
    assert "appium-uiautomator2" in envs
    assert sum(1 for c in runner.calls if c[0] == "install_appium") == 1
    assert mgr.refcount(envs["appium-uiautomator2"].runtime_id) == 1


@pytest.mark.asyncio
async def test_reconcile_treats_already_installed_driver_as_success(tmp_path: object) -> None:
    runner = _AlreadyInstalledRunner()
    mgr = AppiumRuntimeManager(runner=runner, root_dir=tmp_path)  # type: ignore
    envs, errors = await mgr.reconcile({"appium-uiautomator2": _spec()})
    assert "appium-uiautomator2" in envs
    assert errors == {}


@pytest.mark.asyncio
async def test_reconcile_reports_installed_appium_version_after_driver_install(tmp_path: object) -> None:
    runner = _DriverInstallMutatesAppiumRunner()
    mgr = AppiumRuntimeManager(runner=runner, root_dir=tmp_path)  # type: ignore

    envs, errors = await mgr.reconcile({"appium-uiautomator2": _spec(server="2.11.5")})

    assert errors == {}
    assert envs["appium-uiautomator2"].server_version == "2.19.0"
    assert envs["appium-uiautomator2"].driver_versions == {"appium-uiautomator2-driver": "4.2.0"}


@pytest.mark.asyncio
async def test_reconcile_same_mapping_twice_does_not_grow_refcount(tmp_path: object) -> None:
    runner = _FakeRunner()
    mgr = AppiumRuntimeManager(runner=runner, root_dir=tmp_path)  # type: ignore
    await mgr.reconcile({"appium-uiautomator2": _spec()})
    envs, _errors = await mgr.reconcile({"appium-uiautomator2": _spec()})
    rid = envs["appium-uiautomator2"].runtime_id
    assert mgr.refcount(rid) == 1
    assert sum(1 for c in runner.calls if c[0] == "install_appium") == 1


@pytest.mark.asyncio
async def test_reconcile_two_packs_sharing_spec_refcount_two(tmp_path: object) -> None:
    runner = _FakeRunner()
    mgr = AppiumRuntimeManager(runner=runner, root_dir=tmp_path)  # type: ignore
    envs, _errors = await mgr.reconcile({"pack-a": _spec(), "pack-b": _spec()})
    rid_a = envs["pack-a"].runtime_id
    rid_b = envs["pack-b"].runtime_id
    assert rid_a == rid_b
    assert mgr.refcount(rid_a) == 2
    assert sum(1 for c in runner.calls if c[0] == "install_appium") == 1


@pytest.mark.asyncio
async def test_reconcile_removed_pack_decrements_refcount(tmp_path: object) -> None:
    runner = _FakeRunner()
    mgr = AppiumRuntimeManager(runner=runner, root_dir=tmp_path)  # type: ignore
    first_envs, _errors = await mgr.reconcile({"pack-a": _spec(), "pack-b": _spec()})
    rid = first_envs["pack-a"].runtime_id
    assert mgr.refcount(rid) == 2
    await mgr.reconcile({"pack-a": _spec()})
    assert mgr.refcount(rid) == 1


@pytest.mark.asyncio
async def test_reconcile_different_specs_install_separately(tmp_path: object) -> None:
    runner = _FakeRunner()
    mgr = AppiumRuntimeManager(runner=runner, root_dir=tmp_path)  # type: ignore
    envs, _errors = await mgr.reconcile({"pack-a": _spec(server="2.11.5"), "pack-b": _spec(server="2.2.2")})
    assert envs["pack-a"].runtime_id != envs["pack-b"].runtime_id
    assert mgr.refcount(envs["pack-a"].runtime_id) == 1
    assert mgr.refcount(envs["pack-b"].runtime_id) == 1
    assert sum(1 for c in runner.calls if c[0] == "install_appium") == 2


def test_runtime_manager_honors_agent_runtime_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ROOT", "/tmp/runtime-root-from-env")
    from agent_app import config as agent_config
    from agent_app.pack import runtime as pack_runtime

    new_settings = agent_config.AgentSettings()
    monkeypatch.setattr(agent_config, "agent_settings", new_settings)
    monkeypatch.setattr(pack_runtime, "agent_settings", new_settings)

    mgr = AppiumRuntimeManager()
    assert str(mgr._root) == "/tmp/runtime-root-from-env"


def test_runtime_manager_default_root_is_owned_agent_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_RUNTIME_ROOT", raising=False)
    from agent_app import config as agent_config
    from agent_app.pack import runtime as pack_runtime

    new_settings = agent_config.AgentSettings()
    monkeypatch.setattr(agent_config, "agent_settings", new_settings)
    monkeypatch.setattr(pack_runtime, "agent_settings", new_settings)

    mgr = AppiumRuntimeManager()
    assert str(mgr._root) == "/opt/gridfleet-agent/runtimes"


# ── RealNpmRunner error paths ──────────────────────────────────────


@pytest.mark.asyncio
async def test_real_npm_runner_install_appium_failure(tmp_path: Path) -> None:
    runner = RealNpmRunner()
    with patch(
        "agent_app.pack.runtime.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    ) as mock_exec:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"npm ERR! something"))
        proc.returncode = 1
        mock_exec.return_value = proc
        with pytest.raises(RuntimeError, match="appium install failed"):
            await runner.install_appium("appium", "2.11.5", str(tmp_path / "home"))


@pytest.mark.asyncio
async def test_real_npm_runner_install_driver_failure(tmp_path: Path) -> None:
    runner = RealNpmRunner()
    with patch(
        "agent_app.pack.runtime.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    ) as mock_exec:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"stdout", b"stderr"))
        proc.returncode = 1
        mock_exec.return_value = proc
        with pytest.raises(RuntimeError, match="driver install failed"):
            await runner.install_driver(
                "uiautomator2",
                "appium-uiautomator2-driver",
                "3.6.0",
                str(tmp_path / "home"),
            )


@pytest.mark.asyncio
async def test_real_npm_runner_install_plugin_failure(tmp_path: Path) -> None:
    runner = RealNpmRunner()
    with patch(
        "agent_app.pack.runtime.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    ) as mock_exec:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"plugin error"))
        proc.returncode = 1
        mock_exec.return_value = proc
        with pytest.raises(RuntimeError, match="plugin error"):
            await runner.install_plugin(
                "images",
                "1.0.0",
                "npm",
                None,
                str(tmp_path / "home"),
            )


# ── RealNpmRunner success paths ────────────────────────────────────


@pytest.mark.asyncio
async def test_real_npm_runner_install_appium_success(tmp_path: Path) -> None:
    runner = RealNpmRunner()
    appium_home = str(tmp_path / "home")
    with patch(
        "agent_app.pack.runtime.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    ) as mock_exec:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        mock_exec.return_value = proc
        result = await runner.install_appium("appium", "2.11.5", appium_home)
        assert result == str(Path(appium_home) / "node_modules" / ".bin" / "appium")
        assert Path(appium_home).exists()
        mock_exec.assert_called_once()
        args, _kwargs = mock_exec.call_args
        assert args[0] == "npm"
        assert "--save-exact" in args
        assert "appium@2.11.5" in args


@pytest.mark.asyncio
async def test_real_npm_runner_install_driver_github_source(tmp_path: Path) -> None:
    runner = RealNpmRunner()
    appium_home = str(tmp_path / "home")
    (Path(appium_home) / "node_modules" / ".bin").mkdir(parents=True)
    with patch(
        "agent_app.pack.runtime.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    ) as mock_exec:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        mock_exec.return_value = proc
        await runner.install_driver(
            "uiautomator2",
            "appium-uiautomator2-driver",
            "3.6.0",
            appium_home,
            source="github",
            github_repo="appium/appium-uiautomator2-driver",
        )
        assert mock_exec.call_count == 2
        # First call: npm install
        first_call_args, _ = mock_exec.call_args_list[0]
        assert first_call_args[0] == "npm"
        assert "--save-dev" in first_call_args
        assert "git+https://github.com/appium/appium-uiautomator2-driver.git#v3.6.0" in first_call_args
        # Second call: appium driver list --installed
        second_call_args, _ = mock_exec.call_args_list[1]
        assert second_call_args[0] == str(Path(appium_home) / "node_modules" / ".bin" / "appium")
        assert second_call_args[1:4] == ("driver", "list", "--installed")


@pytest.mark.asyncio
async def test_real_npm_runner_install_plugin_success(tmp_path: Path) -> None:
    runner = RealNpmRunner()
    appium_home = str(tmp_path / "home")
    (Path(appium_home) / "node_modules" / ".bin").mkdir(parents=True)
    with patch(
        "agent_app.pack.runtime.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    ) as mock_exec:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        mock_exec.return_value = proc
        await runner.install_plugin(
            "images",
            "1.0.0",
            "npm",
            "@appium/images-plugin",
            appium_home,
        )
        mock_exec.assert_called_once()
        args, _kwargs = mock_exec.call_args
        assert args[0] == str(Path(appium_home) / "node_modules" / ".bin" / "appium")
        assert args[1:4] == ("plugin", "install", "images@1.0.0")


# ── Helper function tests ───────────────────────────────────────────


def test_versioned_already_has_at() -> None:
    assert _versioned("pkg@1.2.3", "2.0.0") == "pkg@1.2.3"


def test_versioned_adds_at() -> None:
    assert _versioned("pkg", "2.0.0") == "pkg@2.0.0"


def test_versioned_scoped_package() -> None:
    assert _versioned("@scope/pkg", "2.0.0") == "@scope/pkg@2.0.0"


def test_github_ref_semver() -> None:
    assert _github_ref("1.2.3") == "v1.2.3"


def test_github_ref_non_semver() -> None:
    assert _github_ref("main") == "main"


def test_github_npm_install_spec_git_prefix() -> None:
    assert (
        _github_npm_install_spec("git+https://github.com/user/repo", "1.0.0")
        == "git+https://github.com/user/repo.git#v1.0.0"
    )


def test_github_npm_install_spec_https_prefix() -> None:
    assert (
        _github_npm_install_spec("https://github.com/user/repo", "1.0.0")
        == "git+https://github.com/user/repo.git#v1.0.0"
    )


def test_github_npm_install_spec_bare_repo() -> None:
    assert _github_npm_install_spec("user/repo", "2.0.0") == "git+https://github.com/user/repo.git#v2.0.0"


def test_github_npm_install_spec_explicit_ref() -> None:
    assert _github_npm_install_spec("user/repo#feature-x", "2.0.0") == "git+https://github.com/user/repo.git#feature-x"


def test_github_npm_install_spec_no_ref() -> None:
    assert _github_npm_install_spec("user/repo", "") == "git+https://github.com/user/repo.git"


def test_driver_install_commands_github_requires_repo() -> None:
    with pytest.raises(ValueError, match="github_repo required"):
        _driver_install_commands("/bin/appium", "/home", "pkg", "1.0.0", "github", None)


def test_driver_install_commands_github() -> None:
    cmds = _driver_install_commands("/bin/appium", "/home", "pkg", "1.0.0", "github", "user/repo")
    assert len(cmds) == 2
    assert cmds[0][0] == "npm"
    assert "--save-dev" in cmds[0]
    assert "git+https://github.com/user/repo.git#v1.0.0" in cmds[0]
    assert cmds[1] == ["/bin/appium", "driver", "list", "--installed"]


def test_driver_install_commands_npm() -> None:
    cmds = _driver_install_commands("/bin/appium", "/home", "pkg", "1.0.0", "npm", None)
    assert cmds == [["/bin/appium", "driver", "install", "--source=npm", "pkg@1.0.0"]]


def test_plugin_install_command_npm_source() -> None:
    assert _plugin_install_command("/bin/appium", "images", "1.0.0", "npm:@appium/images-plugin", None) == [
        "/bin/appium",
        "plugin",
        "install",
        "@appium/images-plugin@1.0.0",
        "--source=npm",
    ]


def test_plugin_install_command_github_source() -> None:
    assert _plugin_install_command("/bin/appium", "images", "1.0.0", "github:user/repo", None) == [
        "/bin/appium",
        "plugin",
        "install",
        "user/repo",
        "--source=github",
    ]


def test_plugin_install_command_github_source_with_package() -> None:
    assert _plugin_install_command("/bin/appium", "images", "1.0.0", "github:user/repo", "pkg-name") == [
        "/bin/appium",
        "plugin",
        "install",
        "user/repo",
        "--source=github",
        "--package=pkg-name",
    ]


def test_plugin_install_command_git_source() -> None:
    assert _plugin_install_command("/bin/appium", "images", "1.0.0", "git:https://github.com/user/repo.git", None) == [
        "/bin/appium",
        "plugin",
        "install",
        "https://github.com/user/repo.git",
        "--source=git",
    ]


def test_plugin_install_command_local_source() -> None:
    assert _plugin_install_command("/bin/appium", "images", "1.0.0", "local:/path/to/plugin", None) == [
        "/bin/appium",
        "plugin",
        "install",
        "/path/to/plugin",
        "--source=local",
    ]


def test_plugin_install_command_default_source() -> None:
    assert _plugin_install_command("/bin/appium", "images", "1.0.0", "npm", None) == [
        "/bin/appium",
        "plugin",
        "install",
        "images@1.0.0",
    ]


def test_plugin_install_command_default_source_no_version_in_name() -> None:
    assert _plugin_install_command("/bin/appium", "images", "1.0.0", "some_source", None) == [
        "/bin/appium",
        "plugin",
        "install",
        "images@1.0.0",
    ]
