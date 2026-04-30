from typing import Any

import pytest

from agent_app.pack.runtime import AppiumRuntimeManager, RuntimeEnv, RuntimeSpec
from agent_app.pack.state import PackStateClient, PackStateLoop


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
                            "grid_slots": ["native"],
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
        self.posted: list[dict[str, Any]] = []

    async def fetch_desired(self) -> dict[str, Any]:
        return self.desired_payload

    async def post_status(self, payload: dict[str, Any]) -> None:
        self.posted.append(payload)


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


class _FakeVersionCatalog:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def versions(self, package: str) -> list[str]:
        self.calls.append(package)
        return {
            "appium": ["2.11.5", "2.11.9"],
            "appium-uiautomator2-driver": ["3.6.0", "3.6.3"],
        }.get(package, [])


@pytest.mark.asyncio
async def test_loop_posts_installed_status_after_reconcile() -> None:
    client = _FakeClient()
    runtime_mgr = _FakeRuntimeMgr()
    loop = PackStateLoop(
        client=client,
        runtime_mgr=runtime_mgr,
        host_id="00000000-0000-0000-0000-000000000001",
    )
    await loop.run_once()
    assert len(client.posted) == 1
    payload = client.posted[0]
    assert any(p["pack_id"] == "appium-uiautomator2" and p["status"] == "installed" for p in payload["packs"])
    assert len(payload["runtimes"]) == 1
    assert payload["runtimes"][0]["status"] == "installed"
    assert "appium_server" in payload["runtimes"][0]
    assert "appium_driver" in payload["runtimes"][0]
    assert "appium_plugins" in payload["runtimes"][0]


@pytest.mark.asyncio
async def test_loop_two_iterations_reuse_runtime() -> None:
    client = _FakeClient()
    runtime_mgr = _FakeRuntimeMgr()
    loop = PackStateLoop(
        client=client,
        runtime_mgr=runtime_mgr,
        host_id="00000000-0000-0000-0000-000000000001",
    )
    await loop.run_once()
    await loop.run_once()
    assert len(runtime_mgr.reconcile_calls) == 2
    assert runtime_mgr.reconcile_calls[0] == runtime_mgr.reconcile_calls[1]


@pytest.mark.asyncio
async def test_state_loop_resolves_latest_patch_with_version_catalog() -> None:
    client = _FakeClient(runtime_policy={"strategy": "latest_patch"})
    runtime_mgr = _FakeRuntimeMgr()
    catalog = _FakeVersionCatalog()
    loop = PackStateLoop(
        client=client,
        runtime_mgr=runtime_mgr,
        host_id="00000000-0000-0000-0000-000000000001",
        version_catalog=catalog,
    )

    await loop.run_once()

    assert catalog.calls == ["appium", "appium-uiautomator2-driver"]
    payload = client.posted[-1]
    assert payload["packs"][0]["status"] == "installed"
    assert payload["packs"][0]["blocked_reason"] is None


class FakeClient(PackStateClient):
    def __init__(self, desired_payload: dict[str, Any]) -> None:
        self._desired_payload = desired_payload
        self.posted_payloads: list[dict[str, Any]] = []

    async def fetch_desired(self) -> dict[str, Any]:
        return self._desired_payload

    async def post_status(self, payload: dict[str, Any]) -> None:
        self.posted_payloads.append(payload)


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
                            "grid_slots": ["native"],
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
        host_id="00000000-0000-0000-0000-000000000001",
    )

    await loop.run_once()

    assert set(runtime_mgr.desired_by_pack) == {"appium-xcuitest"}
    payload = client.posted_payloads[-1]
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
