from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.host_identity import HostIdentity
from agent_app.pack.runtime import AppiumRuntimeManager, RuntimeEnv, RuntimeSpec
from agent_app.pack.state import PackStateClient, PackStateLoop
from tests.pack.fake_worker import FakeWorkerHandle

if TYPE_CHECKING:
    from agent_app.pack.manifest import DesiredPack


def _host_identity(value: str) -> HostIdentity:
    hi = HostIdentity()
    hi.set(value)
    return hi


class _FakeClient(PackStateClient):
    def __init__(self, runtime_policy: dict[str, Any] | None = None) -> None:
        self.desired_payload: dict[str, Any] = {
            "host_id": "00000000-0000-0000-0000-000000000001",
            "packs": [
                {
                    "id": "appium-uiautomator2",
                    "release": "2026.04.0",
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
                            "identity": {"scheme": "android_serial", "scope": "host"},
                            "display_name": "x",
                            "appium_platform_name": "Android",
                            "capabilities": {
                                "stereotype": {"appium:platformName": "Android"},
                                "session_required": [],
                            },
                        }
                    ],
                    "requires": {},
                }
            ],
        }
        if runtime_policy is not None:
            self.desired_payload["packs"][0]["runtime_policy"] = runtime_policy

    async def fetch_desired(self) -> dict[str, Any]:
        return self.desired_payload


class _FakeRuntimeMgr:
    def __init__(self) -> None:
        self.reconcile_calls: list[dict[str, RuntimeSpec]] = []

    async def reconcile(self, desired_by_pack: dict[str, RuntimeSpec]) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
        self.reconcile_calls.append(dict(desired_by_pack))
        out: dict[str, RuntimeEnv] = {}
        for pack_id, spec in desired_by_pack.items():
            rid = AppiumRuntimeManager.runtime_id_for(spec)
            out[pack_id] = RuntimeEnv(
                runtime_id=rid,
                appium_home=f"/fake/{rid}",
                appium_bin=f"/fake/{rid}/node_modules/.bin/appium",
                server_package=spec.server_package,
                server_version=spec.server_version,
            )
        return out, {}


@pytest.mark.asyncio
async def test_loop_posts_installed_status_after_reconcile() -> None:
    client = _FakeClient()
    runtime_mgr = _FakeRuntimeMgr()
    loop = PackStateLoop(
        client=client,
        runtime_mgr=runtime_mgr,
        host_identity=_host_identity("00000000-0000-0000-0000-000000000001"),
    )
    await loop.run_once()
    payload = loop.latest_status()
    assert payload is not None
    assert any(p["pack_id"] == "appium-uiautomator2" and p["status"] == "installed" for p in payload["packs"])
    assert len(payload["runtimes"]) == 1
    assert payload["runtimes"][0]["status"] == "installed"
    assert "appium_server" in payload["runtimes"][0]
    assert "appium_driver" in payload["runtimes"][0]


@pytest.mark.asyncio
async def test_loop_two_iterations_reuse_runtime() -> None:
    client = _FakeClient()
    runtime_mgr = _FakeRuntimeMgr()
    loop = PackStateLoop(
        client=client,
        runtime_mgr=runtime_mgr,
        host_identity=_host_identity("00000000-0000-0000-0000-000000000001"),
    )
    await loop.run_once()
    await loop.run_once()
    assert len(runtime_mgr.reconcile_calls) == 2
    assert runtime_mgr.reconcile_calls[0] == runtime_mgr.reconcile_calls[1]


class FakeClient(PackStateClient):
    def __init__(self, desired_payload: dict[str, Any]) -> None:
        self._desired_payload = desired_payload

    async def fetch_desired(self) -> dict[str, Any]:
        return self._desired_payload


class FakeRuntimeMgr:
    def __init__(self) -> None:
        self.desired_by_pack: dict[str, RuntimeSpec] = {}

    async def reconcile(self, desired_by_pack: dict[str, RuntimeSpec]) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
        self.desired_by_pack = dict(desired_by_pack)
        out: dict[str, RuntimeEnv] = {}
        for pack_id, spec in desired_by_pack.items():
            rid = AppiumRuntimeManager.runtime_id_for(spec)
            out[pack_id] = RuntimeEnv(
                runtime_id=rid,
                appium_home=f"/fake/{rid}",
                appium_bin=f"/fake/{rid}/node_modules/.bin/appium",
                server_package=spec.server_package,
                server_version=spec.server_version,
            )
        return out, {}


@pytest.mark.asyncio
async def test_state_loop_installs_pack_without_probe_family_filtering() -> None:
    client = FakeClient(
        {
            "host_id": "00000000-0000-0000-0000-000000000001",
            "packs": [
                {
                    "id": "appium-xcuitest",
                    "release": "2026.04.0",
                    "appium_server": {
                        "source": "npm",
                        "package": "appium",
                        "version": ">=2.5,<3",
                        "recommended": "2.19.0",
                        "known_bad": [],
                    },
                    "appium_driver": {
                        "source": "npm",
                        "package": "appium-xcuitest-driver",
                        "version": ">=7,<10",
                        "recommended": "9.3.1",
                        "known_bad": [],
                    },
                    "platforms": [
                        {
                            "id": "ios",
                            "automation_name": "XCUITest",
                            "device_types": ["real_device"],
                            "connection_types": ["usb"],
                            "identity": {"scheme": "apple_udid", "scope": "global"},
                            "capabilities": {"stereotype": {"platformName": "iOS"}},
                        }
                    ],
                }
            ],
        }
    )
    runtime_mgr = FakeRuntimeMgr()
    loop = PackStateLoop(
        client=client,
        runtime_mgr=runtime_mgr,
        host_identity=_host_identity("00000000-0000-0000-0000-000000000001"),
    )

    await loop.run_once()

    assert set(runtime_mgr.desired_by_pack) == {"appium-xcuitest"}
    payload = loop.latest_status()
    assert payload is not None
    assert payload["packs"] == [
        {
            "pack_id": "appium-xcuitest",
            "pack_release": "2026.04.0",
            "runtime_id": payload["runtimes"][0]["runtime_id"],
            "status": "installed",
            "resolved_install_spec": {
                "appium_server": "appium@2.19.0",
                "appium_driver": {"appium-xcuitest-driver": "9.3.1"},
            },
            "installer_log_excerpt": "",
            "resolver_version": "1",
            "blocked_reason": None,
        }
    ]


class _MinimalStateAdapter:
    """Loads clean but implements only the required core (no lifecycle_action)."""

    pack_id = "appium-uiautomator2"
    pack_release = "2026.04.0"

    async def discover(self, ctx: object) -> list[object]:
        return []

    async def normalize_device(self, ctx: object) -> object:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_loop_blocks_pack_when_manifest_declares_unimplemented_hook() -> None:
    client = _FakeClient()
    # Manifest promises a lifecycle action the minimal adapter cannot deliver.
    client.desired_payload["packs"][0]["platforms"][0]["lifecycle_actions"] = [{"id": "reconnect"}]
    registry = AdapterRegistry()

    async def _loader(pack: DesiredPack, env: RuntimeEnv) -> None:
        registry.set(pack.id, pack.release, FakeWorkerHandle(_MinimalStateAdapter()))

    loop = PackStateLoop(
        client=client,
        runtime_mgr=_FakeRuntimeMgr(),
        host_identity=_host_identity("00000000-0000-0000-0000-000000000001"),
        adapter_registry=registry,
        adapter_loader=_loader,
    )

    await loop.run_once()

    status = loop.latest_status()
    assert status is not None
    pack_entry = next(p for p in status["packs"] if p["pack_id"] == "appium-uiautomator2")
    assert pack_entry["status"] == "blocked"
    assert "lifecycle_action" in pack_entry["blocked_reason"]
