from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import pytest

from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import SidecarStatus
from agent_app.pack.runtime import RuntimeEnv, RuntimeSpec
from agent_app.pack.state import PackStateLoop

pytestmark = pytest.mark.asyncio


class _Client:
    def __init__(self, packs: list[dict[str, Any]]) -> None:
        self._packs = packs
        self.posted: list[dict[str, Any]] = []

    async def fetch_desired(self) -> dict[str, Any]:
        return {"host_id": "host-1", "packs": self._packs}

    async def post_status(self, payload: dict[str, Any]) -> None:
        self.posted.append(payload)


class _RuntimeMgr:
    async def reconcile(self, desired_by_pack: dict[str, RuntimeSpec]) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
        return {
            pack_id: RuntimeEnv(
                runtime_id=f"rt-{pack_id}",
                appium_home=f"/tmp/{pack_id}",
                appium_bin=f"/tmp/{pack_id}/node_modules/.bin/appium",
                server_package="appium",
                server_version="2.19.0",
                plugin_statuses=[],
            )
            for pack_id in desired_by_pack
        }, {}


@dataclass
class _Adapter:
    pack_id: str = "uploaded-sidecar-pack"
    pack_release: str = "1.0.0"
    calls: list[tuple[str, str]] | None = None

    async def sidecar_lifecycle(self, feature_id: str, action: Literal["start", "stop", "status"]) -> SidecarStatus:
        if self.calls is None:
            self.calls = []
        self.calls.append((feature_id, action))
        return SidecarStatus(ok=True, detail=f"{action} ok", state="running")


def _pack(features: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "uploaded-sidecar-pack",
        "release": "1.0.0",
        "appium_server": {"source": "npm", "package": "appium", "version": ">=2,<3", "recommended": "2.19.0"},
        "appium_driver": {
            "source": "npm",
            "package": "appium-sidecar-driver",
            "version": ">=1,<2",
            "recommended": "1.2.3",
        },
        "platforms": [
            {
                "id": "sidecar_platform",
                "automation_name": "SidecarAutomation",
                "device_types": ["real_device"],
                "connection_types": ["network"],
                "grid_slots": ["native"],
                "capabilities": {"stereotype": {}},
                "identity": {"scheme": "sidecar_id", "scope": "global"},
            }
        ],
        "features": features,
    }


async def test_pack_state_loop_starts_desired_sidecars() -> None:
    adapter = _Adapter()
    registry = AdapterRegistry()
    registry.set("uploaded-sidecar-pack", "1.0.0", adapter)  # type: ignore[arg-type]
    client = _Client([_pack({"tunnel": {"sidecar": {"adapter_hook": "sidecar_lifecycle"}}})])

    from agent_app.pack.sidecar_supervisor import SidecarSupervisor

    supervisor = SidecarSupervisor(poll_interval_seconds=60)
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_RuntimeMgr(),
        host_id="host-1",
        adapter_registry=registry,
        sidecar_supervisor=supervisor,
    )
    try:
        await loop.run_once()
        assert ("tunnel", "start") in (adapter.calls or [])
        assert client.posted[0]["sidecars"][0]["feature_id"] == "tunnel"
        assert client.posted[0]["sidecars"][0]["ok"] is True
    finally:
        await supervisor.shutdown()


async def test_pack_state_loop_stops_removed_sidecars() -> None:
    adapter = _Adapter()
    registry = AdapterRegistry()
    registry.set("uploaded-sidecar-pack", "1.0.0", adapter)  # type: ignore[arg-type]
    from agent_app.pack.sidecar_supervisor import SidecarSupervisor

    supervisor = SidecarSupervisor(poll_interval_seconds=60)
    client = _Client([_pack({"tunnel": {"sidecar": {"adapter_hook": "sidecar_lifecycle"}}})])
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_RuntimeMgr(),
        host_id="host-1",
        adapter_registry=registry,
        sidecar_supervisor=supervisor,
    )
    try:
        await loop.run_once()
        client._packs = [_pack({})]
        await loop.run_once()
        assert ("tunnel", "stop") in (adapter.calls or [])
        assert client.posted[-1]["sidecars"] == []
    finally:
        await supervisor.shutdown()


async def test_pack_state_loop_drops_stale_sidecar_when_adapter_missing() -> None:
    adapter = _Adapter()
    registry = AdapterRegistry()
    registry.set("uploaded-sidecar-pack", "1.0.0", adapter)  # type: ignore[arg-type]
    from agent_app.pack.sidecar_supervisor import SidecarSupervisor

    supervisor = SidecarSupervisor(poll_interval_seconds=60)
    client = _Client([_pack({"tunnel": {"sidecar": {"adapter_hook": "sidecar_lifecycle"}}})])
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_RuntimeMgr(),
        host_id="host-1",
        adapter_registry=registry,
        sidecar_supervisor=supervisor,
    )
    try:
        await loop.run_once()
        assert supervisor.tracked_keys() == {("uploaded-sidecar-pack", "1.0.0", "tunnel")}
        registry.clear()
        client._packs = []

        await loop.run_once()

        assert supervisor.tracked_keys() == set()
        assert ("tunnel", "stop") not in (adapter.calls or [])
    finally:
        await supervisor.shutdown()
