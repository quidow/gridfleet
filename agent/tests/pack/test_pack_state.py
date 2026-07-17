"""Audit tests: lock blocked-per-runtime isolation in agent pack state payloads.

When one runtime reports an install failure, the blocked_reason must be attached only to that pack
entry and must not mark unrelated pack installs as blocked.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import DoctorCheckResult
from agent_app.pack.host_identity import HostIdentity
from agent_app.pack.runtime import AppiumRuntimeManager, RuntimeEnv, RuntimeSpec
from agent_app.pack.runtime_registry import RuntimeRegistry
from agent_app.pack.state import PackStateLoop
from tests.pack.fake_worker import FakeWorkerHandle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _host_identity(value: str) -> HostIdentity:
    hi = HostIdentity()
    hi.set(value)
    return hi


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

    async def fetch_desired(self) -> dict[str, Any]:
        return self._desired


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
        host_identity=_host_identity("00000000-0000-0000-0000-000000000099"),
    )

    await loop.run_once()

    payload = loop.latest_status()
    assert payload is not None
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
        host_identity=_host_identity("00000000-0000-0000-0000-000000000099"),
    )

    await loop.run_once()

    payload = loop.latest_status()
    assert payload is not None
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
        host_identity=_host_identity("00000000-0000-0000-0000-000000000099"),
    )

    await loop.run_once()

    payload = loop.latest_status()
    assert payload is not None
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
async def test_runtime_entry_includes_blocked_reason_key() -> None:
    """Every runtime entry in the posted payload must include the blocked_reason key."""
    client = _FakeClient(_make_desired([_android_pack()]))
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_identity=_host_identity("00000000-0000-0000-0000-000000000099"),
    )

    await loop.run_once()

    payload = loop.latest_status()
    assert payload is not None
    for rt in payload["runtimes"]:
        assert "blocked_reason" in rt, f"runtime {rt.get('runtime_id')} missing blocked_reason"
        assert rt["blocked_reason"] is None  # installed runtimes have no blocked_reason


@pytest.mark.asyncio
async def test_installed_pack_posts_only_adapter_doctor_results() -> None:
    """Without a runtime change, adapter doctor does not run on every iteration."""
    client = _FakeClient(_make_desired([_android_pack()]))
    registry = AdapterRegistry()
    registry.set("appium-uiautomator2", "2026.04.0", FakeWorkerHandle(_DoctorAdapter()))  # type: ignore[arg-type]
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_identity=_host_identity("00000000-0000-0000-0000-000000000099"),
        adapter_registry=registry,
    )

    await loop.run_once()

    payload = loop.latest_status()
    assert payload is not None
    assert payload["doctor"] == []


@pytest.mark.asyncio
async def test_xcuitest_pack_with_no_adapter_posts_empty_doctor_list() -> None:
    """Without a registered adapter, an xcuitest pack contributes no doctor entries."""
    client = _FakeClient(_make_desired([_ios_pack()]))
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_identity=_host_identity("00000000-0000-0000-0000-000000000099"),
    )

    await loop.run_once()

    payload = loop.latest_status()
    assert payload is not None
    assert payload["doctor"] == []


@pytest.mark.asyncio
async def test_adapter_load_failure_blocks_adapter_shipping_pack() -> None:
    """A pack that ships an adapter tarball must not report installed when its
    adapter worker failed to load: node starts for it defer (the worker supplies
    connection caps like appium:udid), so the pack must surface as blocked with
    the load failure instead of a green installed row."""
    pack = _android_pack()
    pack["tarball_sha256"] = "b" * 64
    client = _FakeClient(_make_desired([pack]))
    registry = AdapterRegistry()

    async def failing_loader(pack: object, env: object) -> None:
        raise RuntimeError("tarball missing on disk")

    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_identity=_host_identity("00000000-0000-0000-0000-000000000099"),
        adapter_registry=registry,
        adapter_loader=failing_loader,
    )

    await loop.run_once()

    payload = loop.latest_status()
    assert payload is not None
    pack_entry = next(p for p in payload["packs"] if p["pack_id"] == "appium-uiautomator2")
    assert pack_entry["status"] == "blocked"
    assert pack_entry["blocked_reason"] == "adapter_load_failed"
    assert pack_entry["runtime_id"] is not None
    assert payload["doctor"] == [
        {
            "pack_id": "appium-uiautomator2",
            "check_id": "adapter_load",
            "ok": False,
            "message": "adapter load failed: tarball missing on disk",
        },
    ]


@pytest.mark.asyncio
async def test_reconcile_stamps_runtime_release_for_pack() -> None:
    """A successful reconcile records which release produced the pack's runtime
    env, so the start gate can tell a fresh runtime from one retained across a
    failed or in-flight upgrade."""
    from agent_app.pack.runtime_registry import RuntimeRegistry

    pack = _android_pack(release="2026.04.0")
    client = _FakeClient(_make_desired([pack]))
    runtime_registry = RuntimeRegistry()
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_identity=_host_identity("00000000-0000-0000-0000-000000000099"),
        runtime_registry=runtime_registry,
    )

    await loop.run_once()

    assert runtime_registry.release_for_pack("appium-uiautomator2") == "2026.04.0"


@pytest.mark.asyncio
async def test_adapterless_tarball_pack_reports_installed() -> None:
    """A tarball the loader inspected and marked adapterless (Tier-1
    manifest-only pack) is installed, not blocked — and the loader is not
    re-invoked every tick for it."""
    pack = _android_pack()
    pack["tarball_sha256"] = "c" * 64
    client = _FakeClient(_make_desired([pack]))
    registry = AdapterRegistry()
    loader_calls: list[str] = []

    async def marking_loader(pack: object, env: object) -> None:
        loader_calls.append(pack.id)  # type: ignore[attr-defined]
        registry.mark_adapterless(pack.id, pack.release)  # type: ignore[attr-defined]

    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_identity=_host_identity("00000000-0000-0000-0000-000000000099"),
        adapter_registry=registry,
        adapter_loader=marking_loader,
    )

    await loop.run_once()

    payload = loop.latest_status()
    assert payload is not None
    pack_entry = next(p for p in payload["packs"] if p["pack_id"] == "appium-uiautomator2")
    assert pack_entry["status"] == "installed"
    assert pack_entry["blocked_reason"] is None
    assert payload["doctor"] == []

    await loop.run_once()
    assert loader_calls == ["appium-uiautomator2"]


@pytest.mark.asyncio
async def test_adapterless_pack_with_declared_lifecycle_actions_is_blocked() -> None:
    """The declare-it-then-implement-it rule applies to wheel-less tarballs too:
    a pack whose manifest declares lifecycle_actions but ships no adapter can
    never dispatch the required hook — blocked, not installed."""
    pack = _android_pack()
    pack["tarball_sha256"] = "d" * 64
    pack["platforms"][0]["lifecycle_actions"] = [{"id": "boot"}]
    client = _FakeClient(_make_desired([pack]))
    registry = AdapterRegistry()

    async def marking_loader(pack: object, env: object) -> None:
        registry.mark_adapterless(pack.id, pack.release)  # type: ignore[attr-defined]

    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_identity=_host_identity("00000000-0000-0000-0000-000000000099"),
        adapter_registry=registry,
        adapter_loader=marking_loader,
    )

    await loop.run_once()

    payload = loop.latest_status()
    assert payload is not None
    pack_entry = next(p for p in payload["packs"] if p["pack_id"] == "appium-uiautomator2")
    assert pack_entry["status"] == "blocked"
    assert pack_entry["blocked_reason"] is not None
    assert "lifecycle_action" in pack_entry["blocked_reason"]


@pytest.mark.asyncio
async def test_artifact_less_pack_with_declared_lifecycle_actions_is_blocked() -> None:
    """The declare-it-then-implement-it rule also covers packs with no tarball
    at all: declared lifecycle_actions can never dispatch without an adapter."""
    pack = _android_pack()
    pack["platforms"][0]["lifecycle_actions"] = [{"id": "boot"}]
    client = _FakeClient(_make_desired([pack]))
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_identity=_host_identity("00000000-0000-0000-0000-000000000099"),
        adapter_registry=AdapterRegistry(),
        adapter_loader=None,
    )

    await loop.run_once()

    payload = loop.latest_status()
    assert payload is not None
    pack_entry = next(p for p in payload["packs"] if p["pack_id"] == "appium-uiautomator2")
    assert pack_entry["status"] == "blocked"
    assert pack_entry["blocked_reason"] is not None
    assert "lifecycle_action" in pack_entry["blocked_reason"]


@pytest.mark.asyncio
async def test_adapter_load_failure_surfaces_as_doctor_entry() -> None:
    """A manifest-only pack (no adapter tarball) expects no adapter worker: a
    raising loader still reports installed, with the failure as a doctor entry."""
    client = _FakeClient(_make_desired([_android_pack()]))
    registry = AdapterRegistry()

    async def failing_loader(pack: object, env: object) -> None:
        raise RuntimeError("tarball missing on disk")

    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_identity=_host_identity("00000000-0000-0000-0000-000000000099"),
        adapter_registry=registry,
        adapter_loader=failing_loader,
    )

    await loop.run_once()

    payload = loop.latest_status()
    assert payload is not None
    assert payload["doctor"] == [
        {
            "pack_id": "appium-uiautomator2",
            "check_id": "adapter_load",
            "ok": False,
            "message": "adapter load failed: tarball missing on disk",
        },
    ]
    pack_entry = next(p for p in payload["packs"] if p["pack_id"] == "appium-uiautomator2")
    assert pack_entry["status"] == "installed"


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
        host_identity=_host_identity("00000000-0000-0000-0000-000000000099"),
    )

    await loop.run_once()

    payload = loop.latest_status()
    assert payload is not None
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
        host_identity=_host_identity("00000000-0000-0000-0000-000000000099"),
    )

    await loop.run_once()

    payload = loop.latest_status()
    assert payload is not None
    by_pack = {p["pack_id"]: p for p in payload["packs"]}
    assert by_pack["local-network"]["blocked_reason"] is None


@pytest.mark.asyncio
async def test_doctor_runs_on_first_install() -> None:
    """When a pack's runtime is freshly installed, doctor runs automatically."""
    client = _FakeClient(_make_desired([_android_pack()]))
    registry = AdapterRegistry()
    registry.set("appium-uiautomator2", "2026.04.0", FakeWorkerHandle(_DoctorAdapter()))  # type: ignore[arg-type]
    runtime_registry = RuntimeRegistry()
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_identity=_host_identity("00000000-0000-0000-0000-000000000099"),
        adapter_registry=registry,
        runtime_registry=runtime_registry,
    )

    # First run: runtime is new, doctor should fire
    await loop.run_once()

    payload = loop.latest_status()
    assert payload is not None
    assert payload["doctor"] == [
        {
            "pack_id": "appium-uiautomator2",
            "check_id": "adb",
            "ok": True,
            "message": "host=00000000-0000-0000-0000-000000000099",
        },
    ]

    # Second run: same runtime, doctor should NOT fire
    await loop.run_once()

    payload = loop.latest_status()
    assert payload is not None
    assert payload["doctor"] == []


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
        host_identity=_host_identity("00000000-0000-0000-0000-000000000099"),
    )

    await loop.run_once()

    payload = loop.latest_status()
    assert payload is not None
    by_pack = {p["pack_id"]: p for p in payload["packs"]}
    assert by_pack["local-apple-devicectl"]["blocked_reason"] is None


@pytest.mark.asyncio
async def test_run_once_calls_on_status_once_and_stores_status_without_host_id() -> None:
    calls: list[None] = []
    client = _FakeClient(_make_desired([_android_pack()]))
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_SucceedingRuntimeMgr(),
        host_identity=_host_identity("00000000-0000-0000-0000-000000000099"),
        on_status=lambda: calls.append(None),
    )

    await loop.run_once()

    assert len(calls) == 1
    payload = loop.latest_status()
    assert payload is not None
    assert set(payload) == {"runtimes", "packs", "doctor"}
