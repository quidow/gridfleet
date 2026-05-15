from __future__ import annotations

import asyncio
import contextlib
from typing import Any, ClassVar

import pytest

from agent_app.pack.state import PackStateLoop


def _make_desired(packs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"host_id": "h", "packs": packs}


def _android_pack(pack_id: str = "appium-uiautomator2", release: str = "2026.04.0") -> dict[str, Any]:
    return {
        "id": pack_id,
        "release": release,
        "appium_server": {
            "source": "npm",
            "package": "appium",
            "version": ">=2.5,<3",
            "recommended": "2.11.5",
            "known_bad": [],
        },
        "appium_driver": {
            "source": "npm",
            "package": "appium-uiautomator2-driver",
            "version": ">=3,<5",
            "recommended": "3.6.0",
            "known_bad": [],
        },
        "platforms": [
            {
                "id": "android_mobile",
                "automation_name": "UiAutomator2",
                "device_types": ["real_device"],
                "connection_types": ["usb"],
                "grid_slots": ["native"],
                "identity": {"scheme": "android_serial", "scope": "host"},
                "display_name": "Android",
                "appium_platform_name": "Android",
                "capabilities": {
                    "stereotype": {"appium:platformName": "Android"},
                    "session_required": [],
                },
            }
        ],
        "requires": {},
    }


class _FailingRuntimeMgr:
    async def reconcile(self, desired_by_pack: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        raise RuntimeError("reconcile boom")


class _FakeClient:
    def __init__(self, desired_payload: dict[str, Any]) -> None:
        self._desired = desired_payload
        self.posted: list[dict[str, Any]] = []

    async def fetch_desired(self) -> dict[str, Any]:
        return self._desired

    async def post_status(self, payload: dict[str, Any]) -> None:
        self.posted.append(payload)


@pytest.mark.asyncio
async def test_runtime_reconcile_exception_returns_empty_envs() -> None:
    client = _FakeClient(_make_desired([_android_pack()]))
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_FailingRuntimeMgr(),
        host_id="h",
    )
    await loop.run_once()
    payload = client.posted[-1]
    by_pack = {p["pack_id"]: p for p in payload["packs"]}
    assert by_pack["appium-uiautomator2"]["status"] == "blocked"
    assert by_pack["appium-uiautomator2"]["blocked_reason"] == "runtime_install_failed"


class _CatalogVersion:
    async def versions(self, package: str) -> list[str]:
        return ["3.5.9", "3.6.0", "3.6.1"]


@pytest.mark.asyncio
async def test_latest_patch_uses_version_catalog_when_available_versions_missing() -> None:
    pack = _android_pack()
    # Remove available_versions from installable so version_catalog is hit
    pack["appium_server"]["available_versions"] = ["2.11.5"]
    pack["appium_driver"]["available_versions"] = []  # should trigger catalog
    client = _FakeClient(_make_desired([pack]))

    class _Mgr:
        async def reconcile(self, desired: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
            return {}, {}

    loop = PackStateLoop(
        client=client,
        runtime_mgr=_Mgr(),
        host_id="h",
        version_catalog=_CatalogVersion(),
    )
    await loop.run_once()
    payload = client.posted[-1]
    by_pack = {p["pack_id"]: p for p in payload["packs"]}
    assert by_pack["appium-uiautomator2"]["status"] == "blocked"


class _TrackedSidecarSupervisor:
    def __init__(self) -> None:
        self._keys: set[tuple[str, str, str]] = set()
        self.calls: list[dict[str, object]] = []

    async def start(self, **kwargs: object) -> None:
        self.calls.append({"action": "start", **kwargs})
        self._keys.add((kwargs["pack_id"], kwargs["release"], kwargs["feature_id"]))

    async def stop(self, **kwargs: object) -> None:
        self.calls.append({"action": "stop", **kwargs})
        self._keys.discard((kwargs["pack_id"], kwargs["release"], kwargs["feature_id"]))

    async def drop(self, **kwargs: object) -> None:
        self.calls.append({"action": "drop", **kwargs})
        self._keys.discard((kwargs["pack_id"], kwargs["release"], kwargs["feature_id"]))

    def tracked_keys(self) -> set[tuple[str, str, str]]:
        return self._keys

    def status_snapshot(self) -> list[dict[str, Any]]:
        return []


@pytest.mark.asyncio
async def test_stale_sidecar_dropped_when_adapter_missing() -> None:
    pack = _android_pack()
    pack["sidecar_feature_ids"] = ["feature-a"]
    client = _FakeClient(_make_desired([pack]))

    class _Env:
        runtime_id = "r1"
        server_package = "appium"
        server_version = "2.11.5"
        driver_versions: ClassVar[dict[str, str]] = {}
        plugin_statuses: ClassVar[list[object]] = []
        appium_home = "/tmp"

    class _Mgr:
        async def reconcile(self, desired: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
            return {"appium-uiautomator2": _Env()}, {}

    supervisor = _TrackedSidecarSupervisor()
    supervisor._keys.add(("appium-uiautomator2", "2026.04.0", "old-feature"))
    registry = _Registry()  # empty registry: adapter missing but registry present

    loop = PackStateLoop(
        client=client,
        runtime_mgr=_Mgr(),
        host_id="h",
        sidecar_supervisor=supervisor,
        adapter_registry=registry,
    )
    await loop.run_once()
    drop_calls = [c for c in supervisor.calls if c["action"] == "drop"]
    assert len(drop_calls) == 1
    assert drop_calls[0]["feature_id"] == "old-feature"


class _AdapterWithSidecar:
    pass


class _Registry:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], object] = {}

    def set(self, pack_id: str, release: str, adapter: object) -> None:
        self._data[(pack_id, release)] = adapter

    def get(self, pack_id: str, release: str) -> object | None:
        return self._data.get((pack_id, release))

    def has(self, pack_id: str, release: str) -> bool:
        return (pack_id, release) in self._data

    def get_current(self, pack_id: str) -> object | None:
        return self._data.get((pack_id, ""))


@pytest.mark.asyncio
async def test_stale_sidecar_stopped_when_adapter_present() -> None:
    pack = _android_pack()
    pack["sidecar_feature_ids"] = ["feature-a"]
    client = _FakeClient(_make_desired([pack]))

    class _Env:
        runtime_id = "r1"
        server_package = "appium"
        server_version = "2.11.5"
        driver_versions: ClassVar[dict[str, str]] = {}
        plugin_statuses: ClassVar[list[object]] = []
        appium_home = "/tmp"

    class _Mgr:
        async def reconcile(self, desired: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
            return {"appium-uiautomator2": _Env()}, {}

    supervisor = _TrackedSidecarSupervisor()
    supervisor._keys.add(("appium-uiautomator2", "2026.04.0", "old-feature"))
    registry = _Registry()
    registry.set("appium-uiautomator2", "2026.04.0", _AdapterWithSidecar())

    loop = PackStateLoop(
        client=client,
        runtime_mgr=_Mgr(),
        host_id="h",
        sidecar_supervisor=supervisor,
        adapter_registry=registry,
    )
    await loop.run_once()
    stop_calls = [c for c in supervisor.calls if c["action"] == "stop"]
    assert len(stop_calls) == 1
    assert stop_calls[0]["feature_id"] == "old-feature"


@pytest.mark.asyncio
async def test_run_forever_catches_exception_and_sleeps() -> None:
    class _BadClient:
        async def fetch_desired(self) -> dict[str, Any]:
            raise RuntimeError("fetch boom")

        async def post_status(self, payload: dict[str, Any]) -> None:
            pass

    loop = PackStateLoop(
        client=_BadClient(),
        runtime_mgr=_FailingRuntimeMgr(),
        host_id="h",
        poll_interval=0.01,
    )
    task = asyncio.create_task(loop.run_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)
