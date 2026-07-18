from __future__ import annotations

import asyncio
import time
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Protocol

import pytest

from agent_app.pack.adapter_dispatch import (
    AdapterHookExecutionError,
    AdapterHookTimeoutError,
    dispatch_discover,
    dispatch_health_check,
)
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.contexts import DiscoveryCtx, HealthCtx
from agent_app.pack.runtime import RuntimeEnv, RuntimeSpec
from agent_app.pack.state import PackStateLoop
from agent_app.pack.worker_supervisor import WorkerSupervisor

if TYPE_CHECKING:
    from pathlib import Path


class _Client:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def fetch_desired(self) -> dict[str, Any]:
        return self.payload


class _PackLike(Protocol):
    id: str
    release: str


class _RuntimeMgr:
    async def reconcile(self, desired_by_pack: dict[str, RuntimeSpec]) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
        return {
            pack_id: RuntimeEnv(
                runtime_id=f"runtime-{pack_id}",
                appium_home=f"/tmp/{pack_id}",
                appium_bin="/tmp/appium",
                server_package=spec.server_package,
                server_version=spec.server_version,
            )
            for pack_id, spec in desired_by_pack.items()
        }, {}


def _desired() -> dict[str, Any]:
    packs = []
    for pack_id in ("pack-a", "pack-b"):
        packs.append(
            {
                "id": pack_id,
                "release": "1.0.0",
                "appium_server": {
                    "source": "npm",
                    "package": "appium",
                    "version": ">=2,<3",
                    "recommended": "2.11.5",
                    "known_bad": [],
                },
                "appium_driver": {
                    "source": "npm",
                    "package": f"{pack_id}-driver",
                    "version": ">=1,<2",
                    "recommended": "1.0.0",
                    "known_bad": [],
                },
                "platforms": [
                    {
                        "id": "platform",
                        "automation_name": "generic",
                        "device_types": ["device"],
                        "connection_types": ["network"],
                        "identity": {"scheme": "serial", "scope": "host"},
                        "capabilities": {"stereotype": {}},
                    }
                ],
            }
        )
    return {"host_id": "host", "packs": packs}


def _write_site(site: Path, source: str) -> None:
    package = site / "adapter"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(source)


async def _build_loop(
    tmp_path: Path,
    supervisor: WorkerSupervisor,
    *,
    crashing: bool = False,
) -> tuple[PackStateLoop, AdapterRegistry]:
    sites = {pack_id: tmp_path / pack_id for pack_id in ("pack-a", "pack-b")}
    pack_a_source = (
        """
import os
import time
from pathlib import Path

class Adapter:
    async def health_check(self, ctx):
        marker = Path(__file__).with_name("blocked")
        if not marker.exists():
            marker.touch()
            time.sleep(60)
        return []
    async def discover(self, ctx):
        return []
        """
        if not crashing
        else """
import os
from pathlib import Path

class Adapter:
    async def health_check(self, ctx):
        return []
    async def discover(self, ctx):
        marker = Path(__file__).with_name("crashed")
        if not marker.exists():
            marker.touch()
            os._exit(3)
        return []
"""
    )
    _write_site(sites["pack-a"], pack_a_source)
    _write_site(sites["pack-b"], "class Adapter:\n    async def health_check(self, ctx):\n        return []\n")
    registry = AdapterRegistry()

    async def loader(pack: _PackLike, _env: RuntimeEnv) -> None:
        pack_id = pack.id
        release = pack.release
        registry.set(pack_id, release, await supervisor.start(pack_id, release, sites[pack_id]))

    loop = PackStateLoop(
        client=_Client(_desired()),
        runtime_mgr=_RuntimeMgr(),
        host_identity=type("Host", (), {"get": lambda self: "host"})(),  # type: ignore[call-arg]
        runtime_registry=None,
        adapter_registry=registry,
        adapter_loader=loader,
    )
    await loop.run_once()
    return loop, registry


@pytest.mark.asyncio
async def test_blocking_worker_is_contained_and_other_pack_stays_live(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(hook_timeout=0.15, kill_grace_sec=0.1, restart_delays=(0.02,))
    loop, registry = await _build_loop(tmp_path, supervisor)
    del loop
    ticks: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        while not stop.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.05)

    ticker_task = asyncio.create_task(ticker())
    try:
        handle_a = registry.get("pack-a", "1.0.0")
        handle_b = registry.get("pack-b", "1.0.0")
        assert handle_a is not None and handle_b is not None
        task_a = asyncio.create_task(dispatch_health_check(handle_a, HealthCtx(device_identity_value="a")))
        task_b = asyncio.create_task(dispatch_health_check(handle_b, HealthCtx(device_identity_value="b")))
        with pytest.raises(AdapterHookTimeoutError):
            await task_a
        assert await task_b == []
        assert max((b - a for a, b in pairwise(ticks)), default=0) < 2
        for _ in range(100):
            if handle_a.alive:
                break
            await asyncio.sleep(0.01)
        assert handle_a.alive
        assert await dispatch_health_check(handle_a, HealthCtx(device_identity_value="a")) == []
    finally:
        stop.set()
        await ticker_task
        await supervisor.shutdown_all()


@pytest.mark.asyncio
async def test_crashing_worker_is_reported_and_revives(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(hook_timeout=1, kill_grace_sec=0.1, restart_delays=(0.25,))
    loop, registry = await _build_loop(tmp_path, supervisor, crashing=True)
    handle = registry.get("pack-a", "1.0.0")
    assert handle is not None
    try:
        with pytest.raises(AdapterHookExecutionError):
            await dispatch_discover(handle, DiscoveryCtx(host_id="h", platform_id="p"))
        await loop.run_once()
        pack_entry = next(entry for entry in loop.latest_status()["packs"] if entry["pack_id"] == "pack-a")
        assert pack_entry["status"] == "blocked"
        assert any(entry["check_id"] == "adapter_load" for entry in loop.latest_status()["doctor"])
        for _ in range(100):
            if handle.alive:
                break
            await asyncio.sleep(0.02)
        assert handle.alive
    finally:
        await supervisor.shutdown_all()
