from pathlib import Path

import pytest

from agent_app.pack.runtime import AppiumRuntimeManager, RuntimeSpec


class _FakeRunner:
    def __init__(self, fail_packs: set[str]) -> None:
        self.fail_packs = fail_packs

    async def install_appium(self, package: str, version: str, appium_home: str) -> str:
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
        if package in self.fail_packs:
            raise RuntimeError(f"driver install failed for {package}")

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
async def test_reconcile_returns_envs_for_packs_that_installed(tmp_path: Path) -> None:
    manager = AppiumRuntimeManager(root_dir=tmp_path, runner=_FakeRunner(fail_packs={"roku-driver"}))
    desired = {
        "appium-uiautomator2": RuntimeSpec(
            server_package="appium",
            server_version="2.19.0",
            drivers=(("uiautomator2-driver", "5.0.0", "npm", None),),
            plugins=(),
            node_major=24,
        ),
        "appium-roku": RuntimeSpec(
            server_package="appium",
            server_version="2.19.0",
            drivers=(("roku-driver", "0.1.1", "npm", None),),
            plugins=(),
            node_major=24,
        ),
    }

    envs, errors = await manager.reconcile(desired)

    assert "appium-uiautomator2" in envs
    assert "appium-roku" not in envs
    assert "appium-roku" in errors
    assert "driver install failed for roku-driver" in errors["appium-roku"]


@pytest.mark.asyncio
async def test_reconcile_shared_rid_failure_blocks_all_sharing_packs(tmp_path: Path) -> None:
    """Two packs sharing the same RuntimeSpec (same rid) both land in errors when install fails."""
    manager = AppiumRuntimeManager(root_dir=tmp_path, runner=_FakeRunner(fail_packs={"bad-driver"}))
    shared_spec = RuntimeSpec(
        server_package="appium",
        server_version="2.19.0",
        drivers=(("bad-driver", "1.0.0", "npm", None),),
        plugins=(),
        node_major=None,
    )
    desired = {
        "pack-a": shared_spec,
        "pack-b": shared_spec,
    }

    envs, errors = await manager.reconcile(desired)

    assert "pack-a" not in envs
    assert "pack-b" not in envs
    assert "pack-a" in errors
    assert "pack-b" in errors
    assert errors["pack-a"] == errors["pack-b"]


@pytest.mark.asyncio
async def test_reconcile_failed_rid_not_added_to_refcounts(tmp_path: Path) -> None:
    """Failed rids should not appear in refcounts so the next reconcile retries them."""
    manager = AppiumRuntimeManager(root_dir=tmp_path, runner=_FakeRunner(fail_packs={"bad-driver"}))
    spec = RuntimeSpec(
        server_package="appium",
        server_version="2.19.0",
        drivers=(("bad-driver", "1.0.0", "npm", None),),
        plugins=(),
        node_major=None,
    )
    rid = AppiumRuntimeManager.runtime_id_for(spec)

    _envs, _errors = await manager.reconcile({"pack-x": spec})

    assert manager.refcount(rid) == 0
