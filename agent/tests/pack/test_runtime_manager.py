import json
from pathlib import Path

import pytest

from agent_app.pack.runtime import AppiumRuntimeManager, NpmRunner, RuntimeSpec


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

    agent_config.agent_settings = agent_config.AgentSettings()

    mgr = AppiumRuntimeManager()
    assert str(mgr._root) == "/tmp/runtime-root-from-env"


def test_runtime_manager_default_root_is_owned_agent_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_RUNTIME_ROOT", raising=False)
    from agent_app import config as agent_config

    agent_config.agent_settings = agent_config.AgentSettings()

    mgr = AppiumRuntimeManager()
    assert str(mgr._root) == "/opt/gridfleet-agent/runtimes"
