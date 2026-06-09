"""Reconcile adopts completed runtimes from disk instead of reinstalling.

The runtime dir name is the sha256 of the spec, so a dir matching the
computed runtime_id is by construction the right content for that spec.
Adoption requires the completion marker (written as the last step of a
successful install) and an executable appium binary.
"""

import dataclasses
import json
import os
from pathlib import Path

import pytest

from agent_app.pack.runtime import AppiumRuntimeManager, RuntimeSpec


class _CountingRunner:
    def __init__(self) -> None:
        self.install_appium_calls = 0

    async def install_appium(self, package: str, version: str, appium_home: str) -> str:
        self.install_appium_calls += 1
        bin_path = Path(appium_home) / "node_modules" / ".bin" / "appium"
        bin_path.parent.mkdir(parents=True, exist_ok=True)
        bin_path.write_text("#!/bin/sh\n")
        bin_path.chmod(0o755)
        return str(bin_path)

    async def install_package(self, package: str, version: str, appium_home: str) -> None:
        (Path(appium_home) / "node_modules" / package).mkdir(parents=True, exist_ok=True)

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
        return None


def _spec() -> RuntimeSpec:
    return RuntimeSpec(
        server_package="appium",
        server_version="2.19.0",
        drivers=(("uiautomator2-driver", "5.0.0", "npm", None),),
        plugins=(),
        node_major=24,
    )


def _seed_completed_runtime(root: Path, rid: str) -> Path:
    """Lay out a runtime dir as a prior successful install would have left it."""
    appium_home = root / rid
    bin_path = appium_home / "node_modules" / ".bin" / "appium"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text("#!/bin/sh\n")
    bin_path.chmod(0o755)
    marker = {
        "runtime_id": rid,
        "appium_home": str(appium_home),
        "appium_bin": str(bin_path),
        "server_package": "appium",
        "server_version": "2.19.0",
        "driver_versions": {"uiautomator2-driver": "5.0.0"},
        "plugin_statuses": [],
    }
    (appium_home / ".runtime-complete").write_text(json.dumps(marker))
    return appium_home


@pytest.mark.asyncio
async def test_reconcile_adopts_completed_runtime_from_disk(tmp_path: Path) -> None:
    spec = _spec()
    rid = AppiumRuntimeManager.runtime_id_for(spec)
    _seed_completed_runtime(tmp_path, rid)

    runner = _CountingRunner()
    manager = AppiumRuntimeManager(root_dir=tmp_path, runner=runner)
    envs, errors = await manager.reconcile({"appium-uiautomator2": spec})

    assert errors == {}
    assert runner.install_appium_calls == 0  # adopted, not reinstalled
    env = envs["appium-uiautomator2"]
    assert env.runtime_id == rid
    assert env.server_version == "2.19.0"
    assert env.driver_versions == {"uiautomator2-driver": "5.0.0"}
    assert os.access(env.appium_bin, os.X_OK)


@pytest.mark.asyncio
async def test_reconcile_reinstalls_when_marker_missing(tmp_path: Path) -> None:
    spec = _spec()
    rid = AppiumRuntimeManager.runtime_id_for(spec)
    appium_home = _seed_completed_runtime(tmp_path, rid)
    (appium_home / ".runtime-complete").unlink()  # partial install: no marker

    runner = _CountingRunner()
    manager = AppiumRuntimeManager(root_dir=tmp_path, runner=runner)
    _envs, errors = await manager.reconcile({"appium-uiautomator2": spec})

    assert errors == {}
    assert runner.install_appium_calls == 1


def _seed_runtime_package_marker(appium_home: Path) -> None:
    marker = json.loads((appium_home / ".runtime-complete").read_text())
    marker["runtime_packages"] = [["appium-ios-remotexpc", "0.44.0"]]
    (appium_home / ".runtime-complete").write_text(json.dumps(marker))


@pytest.mark.asyncio
async def test_reconcile_reinstalls_when_declared_runtime_package_missing(tmp_path: Path) -> None:
    spec = dataclasses.replace(_spec(), runtime_packages=(("appium-ios-remotexpc", "0.44.0"),))
    rid = AppiumRuntimeManager.runtime_id_for(spec)
    appium_home = _seed_completed_runtime(tmp_path, rid)
    _seed_runtime_package_marker(appium_home)  # declared, but package dir never created

    runner = _CountingRunner()
    manager = AppiumRuntimeManager(root_dir=tmp_path, runner=runner)
    _envs, errors = await manager.reconcile({"appium-xcuitest": spec})

    assert errors == {}
    assert runner.install_appium_calls == 1  # not adopted: declared package was absent


@pytest.mark.asyncio
async def test_reconcile_adopts_when_declared_runtime_package_present(tmp_path: Path) -> None:
    spec = dataclasses.replace(_spec(), runtime_packages=(("appium-ios-remotexpc", "0.44.0"),))
    rid = AppiumRuntimeManager.runtime_id_for(spec)
    appium_home = _seed_completed_runtime(tmp_path, rid)
    (appium_home / "node_modules" / "appium-ios-remotexpc").mkdir(parents=True)
    _seed_runtime_package_marker(appium_home)

    runner = _CountingRunner()
    manager = AppiumRuntimeManager(root_dir=tmp_path, runner=runner)
    _envs, errors = await manager.reconcile({"appium-xcuitest": spec})

    assert errors == {}
    assert runner.install_appium_calls == 0  # adopted: declared package present


@pytest.mark.asyncio
async def test_reconcile_reinstalls_when_binary_missing(tmp_path: Path) -> None:
    spec = _spec()
    rid = AppiumRuntimeManager.runtime_id_for(spec)
    appium_home = _seed_completed_runtime(tmp_path, rid)
    (appium_home / "node_modules" / ".bin" / "appium").unlink()

    runner = _CountingRunner()
    manager = AppiumRuntimeManager(root_dir=tmp_path, runner=runner)
    _envs, errors = await manager.reconcile({"appium-uiautomator2": spec})

    assert errors == {}
    assert runner.install_appium_calls == 1


@pytest.mark.asyncio
async def test_install_writes_completion_marker(tmp_path: Path) -> None:
    spec = _spec()
    rid = AppiumRuntimeManager.runtime_id_for(spec)

    manager = AppiumRuntimeManager(root_dir=tmp_path, runner=_CountingRunner())
    envs, errors = await manager.reconcile({"appium-uiautomator2": spec})

    assert errors == {}
    marker_path = tmp_path / rid / ".runtime-complete"
    data = json.loads(marker_path.read_text())
    assert data["runtime_id"] == rid
    assert data["appium_bin"] == envs["appium-uiautomator2"].appium_bin


@pytest.mark.asyncio
async def test_adoption_survives_manager_restart(tmp_path: Path) -> None:
    """The agent-restart scenario: first manager installs, a NEW manager
    (fresh in-memory state) adopts from disk without reinstalling."""
    spec = _spec()
    first = AppiumRuntimeManager(root_dir=tmp_path, runner=_CountingRunner())
    await first.reconcile({"appium-uiautomator2": spec})

    runner2 = _CountingRunner()
    second = AppiumRuntimeManager(root_dir=tmp_path, runner=runner2)
    envs, errors = await second.reconcile({"appium-uiautomator2": spec})

    assert errors == {}
    assert runner2.install_appium_calls == 0
    assert envs["appium-uiautomator2"].runtime_id == AppiumRuntimeManager.runtime_id_for(spec)
