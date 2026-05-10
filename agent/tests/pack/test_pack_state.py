"""Audit tests: lock blocked-per-runtime isolation in agent pack state payloads.

When one runtime reports an install failure, the blocked_reason must be attached only to that pack
entry and must not mark unrelated pack installs as blocked.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import DoctorCheckResult
from agent_app.pack.runtime import AppiumRuntimeManager, RuntimeEnv, RuntimeSpec
from agent_app.pack.state import PackStateLoop

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_desired(packs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "host_id": "00000000-0000-0000-0000-000000000099",
        "packs": packs,
    }


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


def _ios_pack(pack_id: str = "appium-xcuitest", release: str = "2026.04.0") -> dict[str, Any]:
    return {
        "id": pack_id,
        "release": release,
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
                "display_name": "iOS",
                "appium_platform_name": "iOS",
                "capabilities": {
                    "stereotype": {"platformName": "iOS"},
                    "session_required": [],
                },
            }
        ],
        "requires": {},
    }


def _generic_pack(
    *,
    pack_id: str,
    platform_id: str,
    driver_package: str,
) -> dict[str, Any]:
    pack = _android_pack(pack_id=pack_id)
    pack["appium_driver"]["package"] = driver_package
    platform = pack["platforms"][0]
    platform["id"] = platform_id
    platform["identity"] = {"scheme": f"{platform_id}_id", "scope": "host"}
    platform["appium_platform_name"] = "Generic"
    platform["automation_name"] = "Generic"
    platform["capabilities"] = {
        "stereotype": {"appium:platformName": "Generic"},
        "session_required": [],
    }
    return pack


class _FakeClient:
    def __init__(self, desired_payload: dict[str, Any]) -> None:
        self._desired = desired_payload
        self.posted: list[dict[str, Any]] = []

    async def fetch_desired(self) -> dict[str, Any]:
        return self._desired

    async def post_status(self, payload: dict[str, Any]) -> None:
        self.posted.append(payload)


class _FailingRuntimeMgr:
    """Fails reconcile for the given pack_ids; succeeds for all others."""

    def __init__(self, failing_pack_ids: set[str]) -> None:
        self._failing = failing_pack_ids

    async def reconcile(self, desired_by_pack: dict[str, RuntimeSpec]) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
        envs: dict[str, RuntimeEnv] = {}
        errors: dict[str, str] = {}
        for pack_id, spec in desired_by_pack.items():
            if pack_id in self._failing:
                errors[pack_id] = f"runtime_install_failed for {pack_id}"
            else:
                rid = AppiumRuntimeManager.runtime_id_for(spec)
                envs[pack_id] = RuntimeEnv(
                    runtime_id=rid,
                    appium_home=f"/fake/{rid}",
                    appium_bin=f"/fake/{rid}/node_modules/.bin/appium",
                    server_package=spec.server_package,
                    server_version=spec.server_version,
                )
        return envs, errors


class _SucceedingRuntimeMgr:
    """Always succeeds for all packs."""

    async def reconcile(self, desired_by_pack: dict[str, RuntimeSpec]) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
        envs: dict[str, RuntimeEnv] = {}
        for pack_id, spec in desired_by_pack.items():
            rid = AppiumRuntimeManager.runtime_id_for(spec)
            envs[pack_id] = RuntimeEnv(
                runtime_id=rid,
                appium_home=f"/fake/{rid}",
                appium_bin=f"/fake/{rid}/node_modules/.bin/appium",
                server_package=spec.server_package,
                server_version=spec.server_version,
            )
        return envs, {}


class _DoctorAdapter:
    pack_id = "appium-uiautomator2"
    pack_release = "2026.04.0"

    async def doctor(self, ctx: object) -> list[DoctorCheckResult]:
        return [DoctorCheckResult(check_id="adb", ok=True, message=f"host={ctx.host_id}")]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_pack_has_blocked_reason_attached() -> None:
    """When a runtime install fails for pack A, pack A gets blocked_reason; it's not None."""
    client = _FakeClient(_make_desired([_android_pack()]))
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_FailingRuntimeMgr({"appium-uiautomator2"}),
        host_id="00000000-0000-0000-0000-000000000099",
    )

    await loop.run_once()

    payload = client.posted[-1]
    by_pack = {p["pack_id"]: p for p in payload["packs"]}
    assert "appium-uiautomator2" in by_pack
    assert by_pack["appium-uiautomator2"]["status"] == "blocked"
    assert by_pack["appium-uiautomator2"]["blocked_reason"] is not None
    assert by_pack["appium-uiautomator2"]["runtime_id"] is None


@pytest.mark.asyncio
async def test_blocked_runtime_does_not_contaminate_installed_pack() -> None:
    """When pack B's runtime fails, pack A (different runtime) remains installed."""
    client = _FakeClient(_make_desired([_android_pack("appium-uiautomator2"), _ios_pack("appium-xcuitest")]))
    # iOS pack's runtime will fail; Android pack's runtime succeeds.
    # Note: both packs may map to different runtime specs because they have different
    # driver packages, so they get different runtime IDs.
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_FailingRuntimeMgr({"appium-xcuitest"}),
        host_id="00000000-0000-0000-0000-000000000099",
    )

    await loop.run_once()

    payload = client.posted[-1]
    by_pack = {p["pack_id"]: p for p in payload["packs"]}

    # Android pack installed, iOS pack blocked
    assert by_pack["appium-uiautomator2"]["status"] == "installed"
    assert by_pack["appium-uiautomator2"]["blocked_reason"] is None
    assert by_pack["appium-uiautomator2"]["runtime_id"] is not None

    assert by_pack["appium-xcuitest"]["status"] == "blocked"
    assert by_pack["appium-xcuitest"]["blocked_reason"] is not None
    assert by_pack["appium-xcuitest"]["runtime_id"] is None


@pytest.mark.asyncio
async def test_installed_packs_are_not_blocked_by_host_probe_support() -> None:
    """Adapter-only packs do not depend on legacy host probe-family support."""
    client = _FakeClient(_make_desired([_android_pack("appium-uiautomator2"), _ios_pack("appium-xcuitest")]))
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_id="00000000-0000-0000-0000-000000000099",
    )

    await loop.run_once()

    payload = client.posted[-1]
    by_pack = {p["pack_id"]: p for p in payload["packs"]}

    # Android pack installed normally
    assert by_pack["appium-uiautomator2"]["status"] == "installed"
    assert by_pack["appium-uiautomator2"]["blocked_reason"] is None

    assert by_pack["appium-xcuitest"]["status"] == "installed"
    assert by_pack["appium-xcuitest"]["blocked_reason"] is None
    assert by_pack["appium-xcuitest"]["runtime_id"] is not None

    runtime_ids = {rt["runtime_id"] for rt in payload["runtimes"]}
    assert all(rt["status"] == "installed" for rt in payload["runtimes"])
    assert by_pack["appium-uiautomator2"]["runtime_id"] in runtime_ids
    assert by_pack["appium-xcuitest"]["runtime_id"] in runtime_ids


@pytest.mark.asyncio
async def test_runtime_entry_includes_appium_plugins_key() -> None:
    """Every runtime entry in the posted payload must include the appium_plugins key."""
    client = _FakeClient(_make_desired([_android_pack()]))
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_id="00000000-0000-0000-0000-000000000099",
    )

    await loop.run_once()

    payload = client.posted[-1]
    assert len(payload["runtimes"]) >= 1
    for rt in payload["runtimes"]:
        assert "appium_plugins" in rt, f"runtime {rt.get('runtime_id')} missing appium_plugins"


@pytest.mark.asyncio
async def test_runtime_entry_includes_blocked_reason_key() -> None:
    """Every runtime entry in the posted payload must include the blocked_reason key."""
    client = _FakeClient(_make_desired([_android_pack()]))
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_id="00000000-0000-0000-0000-000000000099",
    )

    await loop.run_once()

    payload = client.posted[-1]
    for rt in payload["runtimes"]:
        assert "blocked_reason" in rt, f"runtime {rt.get('runtime_id')} missing blocked_reason"
        assert rt["blocked_reason"] is None  # installed runtimes have no blocked_reason


@pytest.mark.asyncio
async def test_installed_pack_posts_only_adapter_doctor_results() -> None:
    """PackStateLoop posts only adapter-sourced doctor entries; the generic appium driver doctor is gone."""
    client = _FakeClient(_make_desired([_android_pack()]))
    registry = AdapterRegistry()
    registry.set("appium-uiautomator2", "2026.04.0", _DoctorAdapter())  # type: ignore[arg-type]
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_id="00000000-0000-0000-0000-000000000099",
        adapter_registry=registry,
    )

    await loop.run_once()

    payload = client.posted[-1]
    assert payload["doctor"] == [
        {
            "pack_id": "appium-uiautomator2",
            "check_id": "adb",
            "ok": True,
            "message": "host=00000000-0000-0000-0000-000000000099",
        },
    ]


@pytest.mark.asyncio
async def test_xcuitest_pack_with_no_adapter_posts_empty_doctor_list() -> None:
    """Without a registered adapter, an xcuitest pack contributes no doctor entries.

    No driver_doctor_runner is consulted.
    """
    client = _FakeClient(_make_desired([_ios_pack()]))
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_id="00000000-0000-0000-0000-000000000099",
    )

    await loop.run_once()

    payload = client.posted[-1]
    assert payload["doctor"] == []


@pytest.mark.asyncio
async def test_manual_pack_is_not_blocked_as_unsupported() -> None:
    client = _FakeClient(
        _make_desired(
            [
                _generic_pack(
                    pack_id="local-manual",
                    platform_id="manual_real",
                    driver_package="appium-manual-driver",
                )
            ]
        )
    )
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_id="00000000-0000-0000-0000-000000000099",
    )

    await loop.run_once()

    payload = client.posted[-1]
    by_pack = {p["pack_id"]: p for p in payload["packs"]}
    assert by_pack["local-manual"]["blocked_reason"] is None


@pytest.mark.asyncio
async def test_network_endpoint_pack_is_not_blocked_as_unsupported() -> None:
    client = _FakeClient(
        _make_desired(
            [
                _generic_pack(
                    pack_id="local-network",
                    platform_id="network_real",
                    driver_package="appium-network-driver",
                )
            ]
        )
    )
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_id="00000000-0000-0000-0000-000000000099",
    )

    await loop.run_once()

    payload = client.posted[-1]
    by_pack = {p["pack_id"]: p for p in payload["packs"]}
    assert by_pack["local-network"]["blocked_reason"] is None


@pytest.mark.asyncio
async def test_apple_devicectl_pack_is_not_blocked_as_unsupported() -> None:
    client = _FakeClient(
        _make_desired(
            [
                _generic_pack(
                    pack_id="local-apple-devicectl",
                    platform_id="ios_legacy_real",
                    driver_package="appium-ios-legacy-driver",
                )
            ]
        )
    )
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_id="00000000-0000-0000-0000-000000000099",
    )

    await loop.run_once()

    payload = client.posted[-1]
    by_pack = {p["pack_id"]: p for p in payload["packs"]}
    assert by_pack["local-apple-devicectl"]["blocked_reason"] is None
