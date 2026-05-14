"""Adapter wiring tests.

These tests cover the adapter-only pack contract: when a corresponding
adapter is registered in the per-process :class:`AdapterRegistry`, the
agent's discovery / health / lifecycle / session entry points dispatch
through that adapter.

The tests use a hand-rolled fake adapter that records every call so the
tests stay independent of the asyncio dispatch wrapper details (those
are covered by ``test_adapter_dispatch.py``).
"""

from __future__ import annotations

from typing import Any, Literal, cast

import pytest

from agent_app.appium.process import AppiumProcessManager
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import (
    DiscoveryCandidate,
    DoctorCheckResult,
    DoctorContext,
    FeatureActionResult,
    HardwareTelemetry,
    HealthCheckResult,
    LifecycleActionResult,
    NormalizedDevice,
    SessionOutcome,
    SessionSpec,
    SidecarStatus,
)
from agent_app.pack.discovery import enumerate_pack_candidates, pack_device_properties
from agent_app.pack.dispatch import (
    adapter_health_check,
    adapter_lifecycle_action,
    adapter_normalize_device,
    adapter_post_session,
    adapter_pre_session,
)
from agent_app.pack.manifest import AppiumInstallable, DesiredPack, DesiredPlatform


class _RecordingAdapter:
    """Fake DriverPackAdapter that records calls instead of doing real work."""

    def __init__(self, pack_id: str = "vendor-foo", pack_release: str = "0.1.0") -> None:
        self.pack_id = pack_id
        self.pack_release = pack_release
        self.discovery_scope = ""
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def discover(self, ctx: object) -> list[DiscoveryCandidate]:
        self.calls.append(("discover", {"ctx": ctx}))
        return [
            DiscoveryCandidate(
                identity_scheme="vendor_serial",
                identity_value="VENDOR-1",
                suggested_name="Vendor Device",
                detected_properties={"manufacturer": "Vendor"},
                runnable=True,
                missing_requirements=[],
                field_errors=[],
                feature_status=[],
            )
        ]

    async def doctor(self, ctx: DoctorContext) -> list[DoctorCheckResult]:  # pragma: no cover - unused here
        self.calls.append(("doctor", {"ctx": ctx}))
        return []

    async def health_check(self, ctx: object) -> list[HealthCheckResult]:
        self.calls.append(("health_check", {"ctx": ctx}))
        return [HealthCheckResult(check_id="adapter_alive", ok=True, detail="online")]

    async def lifecycle_action(
        self,
        action_id: Literal["reconnect", "boot", "shutdown", "state"],
        args: dict[str, Any],
        ctx: object,
    ) -> LifecycleActionResult:
        self.calls.append(("lifecycle_action", {"action": action_id, "args": args, "ctx": ctx}))
        return LifecycleActionResult(ok=True, state="running", detail="adapter dispatched")

    async def pre_session(self, spec: SessionSpec) -> dict[str, Any]:
        self.calls.append(("pre_session", {"spec": spec}))
        return {"appium:vendorMagic": "enabled"}

    async def post_session(self, spec: SessionSpec, outcome: SessionOutcome) -> None:
        self.calls.append(("post_session", {"spec": spec, "outcome": outcome}))

    async def feature_action(
        self, feature_id: str, action_id: str, args: dict[str, Any], ctx: object
    ) -> FeatureActionResult:
        self.calls.append(("feature_action", {"feature_id": feature_id, "action_id": action_id, "args": args}))
        return FeatureActionResult(ok=True, detail="adapter dispatched", data={})

    async def sidecar_lifecycle(self, feature_id: str, action: Literal["start", "stop", "status"]) -> SidecarStatus:
        self.calls.append(("sidecar_lifecycle", {"feature_id": feature_id, "action": action}))
        return SidecarStatus(ok=True, detail="running", state="running")

    async def normalize_device(self, ctx: object) -> NormalizedDevice:
        raw_input = cast("Any", ctx).raw_input
        self.calls.append(("normalize_device", {"ctx": ctx, "raw_input": raw_input}))
        return NormalizedDevice(
            identity_scheme="vendor_serial",
            identity_scope="global",
            identity_value="VENDOR-1",
            connection_target=str(raw_input["connection_target"]),
            ip_address=str(raw_input["connection_target"]),
            device_type="real_device",
            connection_type="network",
            os_version="15",
            field_errors=[],
            manufacturer="Vendor",
            model="Model 1",
            model_number="MODEL-1",
            software_versions={"firmware": "15.1.4", "build": "3321"},
        )

    async def telemetry(self, ctx: object) -> HardwareTelemetry:
        self.calls.append(("telemetry", {"ctx": ctx}))
        return HardwareTelemetry(supported=False)


def _make_adapter_pack(pack_id: str = "vendor-foo", release: str = "0.1.0") -> DesiredPack:
    installable = AppiumInstallable("npm", "appium", "2.11.5", None, [])
    driver = AppiumInstallable("npm", "vendor-driver", "0.1.0", None, [])
    return DesiredPack(
        id=pack_id,
        release=release,
        appium_server=installable,
        appium_driver=driver,
        platforms=[
            DesiredPlatform(
                id="vendor_real",
                automation_name="VendorDriver",
                device_types=["real_device"],
                connection_types=["network"],
                grid_slots=["native"],
                identity_scheme="vendor_serial",
                identity_scope="global",
                stereotype={},
            )
        ],
    )


def _make_multi_platform_adapter_pack(pack_id: str = "vendor-foo", release: str = "0.1.0") -> DesiredPack:
    installable = AppiumInstallable("npm", "appium", "2.11.5", None, [])
    driver = AppiumInstallable("npm", "vendor-driver", "0.1.0", None, [])
    return DesiredPack(
        id=pack_id,
        release=release,
        appium_server=installable,
        appium_driver=driver,
        platforms=[
            DesiredPlatform(
                id="android_mobile",
                automation_name="UiAutomator2",
                device_types=["real_device"],
                connection_types=["usb", "network"],
                grid_slots=["native"],
                identity_scheme="android_serial",
                identity_scope="host",
                stereotype={},
            ),
            DesiredPlatform(
                id="android_mobile",
                automation_name="UiAutomator2",
                device_types=["emulator"],
                connection_types=["virtual"],
                grid_slots=["native"],
                identity_scheme="android_serial",
                identity_scope="host",
                stereotype={},
            ),
        ],
    )


@pytest.mark.asyncio
async def test_adapter_kind_discovery_dispatches_to_adapter() -> None:
    """Pack discovery routes through the loaded adapter."""

    pack = _make_adapter_pack()
    adapter = _RecordingAdapter(pack_id=pack.id, pack_release=pack.release)
    registry = AdapterRegistry()
    registry.set(pack.id, pack.release, adapter)

    result = await enumerate_pack_candidates(
        [pack],
        adapter_registry=registry,
        host_id="host-1",
    )
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["pack_id"] == pack.id
    assert candidate["platform_id"] == "vendor_real"
    assert candidate["identity_value"] == "VENDOR-1"
    assert any(call[0] == "discover" for call in adapter.calls)


@pytest.mark.asyncio
async def test_pack_wide_discovery_routes_candidates_to_matching_platforms_once() -> None:
    pack = _make_multi_platform_adapter_pack()
    adapter = _RecordingAdapter(pack_id=pack.id, pack_release=pack.release)
    adapter.discovery_scope = "pack"

    async def discover_all(ctx: object) -> list[DiscoveryCandidate]:
        adapter.calls.append(("discover", {"ctx": ctx}))
        return [
            DiscoveryCandidate(
                identity_scheme="android_serial",
                identity_value="REAL-1",
                suggested_name="Pixel",
                detected_properties={
                    "device_type": "real_device",
                    "connection_type": "usb",
                    "connection_target": "REAL-1",
                },
                runnable=True,
                missing_requirements=[],
                field_errors=[],
                feature_status=[],
            ),
            DiscoveryCandidate(
                identity_scheme="android_serial",
                identity_value="avd:Pixel_8_API_34",
                suggested_name="Pixel 8 API 34",
                detected_properties={
                    "device_type": "emulator",
                    "connection_type": "virtual",
                    "connection_target": "emulator-5554",
                },
                runnable=True,
                missing_requirements=[],
                field_errors=[],
                feature_status=[],
            ),
        ]

    adapter.discover = discover_all  # type: ignore[method-assign]
    registry = AdapterRegistry()
    registry.set(pack.id, pack.release, adapter)

    result = await enumerate_pack_candidates(
        [pack],
        adapter_registry=registry,
        host_id="host-1",
    )

    assert [call[0] for call in adapter.calls] == ["discover"]
    assert [(candidate["identity_value"], candidate["platform_id"]) for candidate in result["candidates"]] == [
        ("REAL-1", "android_mobile"),
        ("avd:Pixel_8_API_34", "android_mobile"),
    ]


@pytest.mark.asyncio
async def test_adapter_kind_discovery_without_adapter_returns_empty() -> None:
    """When no adapter is registered, discovery yields zero candidates."""

    pack = _make_adapter_pack()
    registry = AdapterRegistry()

    result = await enumerate_pack_candidates(
        [pack],
        adapter_registry=registry,
        host_id="host-1",
    )
    assert result["candidates"] == []


@pytest.mark.asyncio
async def test_pack_device_properties_falls_back_to_adapter_normalize_for_endpoint() -> None:
    pack = _make_adapter_pack()
    adapter = _RecordingAdapter(pack_id=pack.id, pack_release=pack.release)

    async def empty_discover(ctx: object) -> list[DiscoveryCandidate]:
        adapter.calls.append(("discover", {"ctx": ctx}))
        return []

    adapter.discover = empty_discover  # type: ignore[method-assign]
    registry = AdapterRegistry()
    registry.set(pack.id, pack.release, adapter)

    result = await pack_device_properties(
        "192.168.1.50",
        pack.id,
        [pack],
        adapter_registry=registry,
        host_id="host-1",
    )

    assert result is not None
    assert result["identity_value"] == "VENDOR-1"
    assert result["connection_target"] == "192.168.1.50"
    assert result["detected_properties"]["os_version"] == "15"
    assert any(call[0] == "normalize_device" for call in adapter.calls)


@pytest.mark.asyncio
async def test_adapter_normalize_device_preserves_extended_identity_fields() -> None:
    pack = _make_adapter_pack()
    adapter = _RecordingAdapter(pack_id=pack.id, pack_release=pack.release)
    registry = AdapterRegistry()
    registry.set(pack.id, pack.release, adapter)

    result = await adapter_normalize_device(
        adapter_registry=registry,
        pack_id=pack.id,
        pack_release=pack.release,
        host_id="host-1",
        platform_id="vendor_real",
        raw_input={"connection_target": "192.168.1.50"},
    )

    assert result is not None
    assert result["model"] == "Model 1"
    assert result["model_number"] == "MODEL-1"
    assert result["software_versions"] == {"firmware": "15.1.4", "build": "3321"}


@pytest.mark.asyncio
async def test_health_check_dispatches_to_adapter() -> None:
    """``adapter_health_check`` consults the registry and returns probe-shaped payload."""

    pack = _make_adapter_pack()
    adapter = _RecordingAdapter(pack_id=pack.id, pack_release=pack.release)
    registry = AdapterRegistry()
    registry.set(pack.id, pack.release, adapter)

    payload = await adapter_health_check(
        adapter_registry=registry,
        pack_id=pack.id,
        pack_release=pack.release,
        identity_value="VENDOR-1",
        allow_boot=False,
        platform_id="tvos",
    )
    assert payload is not None
    assert payload["healthy"] is True
    assert payload["checks"][0]["check_id"] == "adapter_alive"
    health_call = next(call for call in adapter.calls if call[0] == "health_check")
    assert health_call[1]["ctx"].platform_id == "tvos"


@pytest.mark.asyncio
async def test_health_check_without_adapter_returns_none() -> None:
    registry = AdapterRegistry()
    payload = await adapter_health_check(
        adapter_registry=registry,
        pack_id="vendor-foo",
        pack_release="0.1.0",
        identity_value="VENDOR-1",
        allow_boot=False,
    )
    assert payload is None


@pytest.mark.asyncio
async def test_lifecycle_action_dispatches_to_adapter() -> None:
    pack = _make_adapter_pack()
    adapter = _RecordingAdapter(pack_id=pack.id, pack_release=pack.release)
    registry = AdapterRegistry()
    registry.set(pack.id, pack.release, adapter)

    payload = await adapter_lifecycle_action(
        adapter_registry=registry,
        pack_id=pack.id,
        pack_release=pack.release,
        host_id="host-1",
        identity_value="VENDOR-1",
        action="reconnect",
        args={"force": True},
    )
    assert payload is not None
    assert payload["success"] is True
    assert payload["state"] == "running"
    assert payload["detail"] == "adapter dispatched"
    assert any(c[0] == "lifecycle_action" and c[1]["action"] == "reconnect" for c in adapter.calls)


@pytest.mark.asyncio
async def test_lifecycle_action_without_adapter_returns_none() -> None:
    registry = AdapterRegistry()
    payload = await adapter_lifecycle_action(
        adapter_registry=registry,
        pack_id="vendor-foo",
        pack_release="0.1.0",
        host_id="host-1",
        identity_value="VENDOR-1",
        action="reconnect",
        args={},
    )
    assert payload is None


@pytest.mark.asyncio
async def test_pre_session_caps_are_merged() -> None:
    """``adapter_pre_session`` returns extra caps that AppiumProcessManager merges in."""

    pack = _make_adapter_pack()
    adapter = _RecordingAdapter(pack_id=pack.id, pack_release=pack.release)
    registry = AdapterRegistry()
    registry.set(pack.id, pack.release, adapter)

    extra = await adapter_pre_session(
        adapter_registry=registry,
        pack_id=pack.id,
        pack_release=pack.release,
        platform_id="vendor_real",
        identity_value="VENDOR-1",
        capabilities={"baseline": True},
    )
    assert extra == {"appium:vendorMagic": "enabled"}

    # Verify the AppiumProcessManager wires the registry through and merges
    # the adapter caps into ``extra_caps`` *before* building the launch spec.
    mgr = AppiumProcessManager()
    mgr.set_adapter_registry(registry)
    merged = await _simulate_caps_merge(
        mgr=mgr,
        registry=registry,
        pack_id=pack.id,
        platform_id="vendor_real",
        connection_target="VENDOR-1",
        existing_extra_caps={"appium:noReset": True},
    )
    assert merged["appium:noReset"] is True
    assert merged["appium:vendorMagic"] == "enabled"


@pytest.mark.asyncio
async def test_post_session_dispatches_to_adapter() -> None:
    pack = _make_adapter_pack()
    adapter = _RecordingAdapter(pack_id=pack.id, pack_release=pack.release)
    registry = AdapterRegistry()
    registry.set(pack.id, pack.release, adapter)

    dispatched = await adapter_post_session(
        adapter_registry=registry,
        pack_id=pack.id,
        pack_release=pack.release,
        platform_id="vendor_real",
        identity_value="VENDOR-1",
        ok=True,
        detail="closed",
    )
    assert dispatched is True
    assert any(c[0] == "post_session" for c in adapter.calls)


@pytest.mark.asyncio
async def test_state_loop_invokes_adapter_loader_for_adapter_packs() -> None:
    """The state loop calls the adapter loader after a successful runtime install."""

    from agent_app.pack.runtime import AppiumRuntimeManager, RuntimeEnv, RuntimeSpec
    from agent_app.pack.runtime_registry import RuntimeRegistry
    from agent_app.pack.state import PackStateClient, PackStateLoop

    desired_payload: dict[str, Any] = {
        "host_id": "00000000-0000-0000-0000-000000000001",
        "packs": [
            {
                "id": "vendor-foo",
                "release": "0.1.0",
                "tarball_sha256": "a" * 64,
                "appium_server": {
                    "source": "npm",
                    "package": "appium",
                    "version": ">=2.5,<3",
                    "recommended": "2.11.5",
                    "known_bad": [],
                },
                "appium_driver": {
                    "source": "npm",
                    "package": "vendor-driver",
                    "version": ">=0.1,<1",
                    "recommended": "0.1.0",
                    "known_bad": [],
                },
                "platforms": [
                    {
                        "id": "vendor_real",
                        "automation_name": "VendorDriver",
                        "device_types": ["real_device"],
                        "connection_types": ["network"],
                        "grid_slots": ["native"],
                        "identity": {"scheme": "vendor_serial", "scope": "global"},
                        "capabilities": {"stereotype": {}},
                    }
                ],
            }
        ],
    }

    class _FakeClient(PackStateClient):
        def __init__(self) -> None:
            self.posted: list[dict[str, Any]] = []

        async def fetch_desired(self) -> dict[str, Any]:
            return desired_payload

        async def post_status(self, payload: dict[str, Any]) -> None:
            self.posted.append(payload)

    class _FakeRuntimeMgr:
        async def reconcile(
            self, desired_by_pack: dict[str, RuntimeSpec]
        ) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
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

    loader_calls: list[tuple[str, str]] = []
    registry = AdapterRegistry()

    async def _loader(pack: DesiredPack, env: RuntimeEnv) -> None:
        loader_calls.append((pack.id, pack.release))
        registry.set(pack.id, pack.release, _RecordingAdapter(pack.id, pack.release))

    loop = PackStateLoop(
        client=_FakeClient(),
        runtime_mgr=_FakeRuntimeMgr(),
        host_id="00000000-0000-0000-0000-000000000001",
        runtime_registry=RuntimeRegistry(),
        adapter_registry=registry,
        adapter_loader=_loader,
    )

    await loop.run_once()
    assert loader_calls == [("vendor-foo", "0.1.0")]
    assert registry.has("vendor-foo", "0.1.0")

    # Second iteration should not re-invoke the loader for the same release.
    await loop.run_once()
    assert loader_calls == [("vendor-foo", "0.1.0")]


@pytest.mark.asyncio
async def test_state_loop_does_not_block_adapter_packs_on_host_probe_support() -> None:
    """Adapter packs are installed without host probe-family filtering."""

    from agent_app.pack.runtime import AppiumRuntimeManager, RuntimeEnv, RuntimeSpec
    from agent_app.pack.state import PackStateClient, PackStateLoop

    desired_payload: dict[str, Any] = {
        "host_id": "00000000-0000-0000-0000-000000000001",
        "packs": [
            {
                "id": "vendor-foo",
                "release": "0.1.0",
                "appium_server": {
                    "source": "npm",
                    "package": "appium",
                    "version": ">=2.5,<3",
                    "recommended": "2.11.5",
                    "known_bad": [],
                },
                "appium_driver": {
                    "source": "npm",
                    "package": "vendor-driver",
                    "version": ">=0.1,<1",
                    "recommended": "0.1.0",
                    "known_bad": [],
                },
                "platforms": [
                    {
                        "id": "vendor_real",
                        "automation_name": "VendorDriver",
                        "device_types": ["real_device"],
                        "connection_types": ["network"],
                        "grid_slots": ["native"],
                        "identity": {"scheme": "vendor_serial", "scope": "global"},
                        "capabilities": {"stereotype": {}},
                    }
                ],
            }
        ],
    }

    class _FakeClient(PackStateClient):
        def __init__(self) -> None:
            self.posted: list[dict[str, Any]] = []

        async def fetch_desired(self) -> dict[str, Any]:
            return desired_payload

        async def post_status(self, payload: dict[str, Any]) -> None:
            self.posted.append(payload)

    class _FakeRuntimeMgr:
        async def reconcile(
            self, desired_by_pack: dict[str, RuntimeSpec]
        ) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
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

    fake_client = _FakeClient()
    loop = PackStateLoop(
        client=fake_client,
        runtime_mgr=_FakeRuntimeMgr(),
        host_id="00000000-0000-0000-0000-000000000001",
    )
    await loop.run_once()
    pack_entry = fake_client.posted[-1]["packs"][0]
    assert pack_entry["pack_id"] == "vendor-foo"
    assert pack_entry["status"] == "installed"


async def _simulate_caps_merge(
    *,
    mgr: AppiumProcessManager,
    registry: AdapterRegistry,
    pack_id: str,
    platform_id: str,
    connection_target: str,
    existing_extra_caps: dict[str, Any],
) -> dict[str, Any]:
    """Mirror the adapter-merge block inside AppiumProcessManager.start.

    Keeps the wiring test focussed on the merge contract instead of
    spinning up an Appium subprocess.
    """

    merged = dict(existing_extra_caps)
    adapter = registry.get_current(pack_id)
    assert adapter is not None
    adapter_caps = await adapter_pre_session(
        adapter_registry=registry,
        pack_id=pack_id,
        pack_release=adapter.pack_release,
        platform_id=platform_id,
        identity_value=connection_target,
        capabilities=merged,
    )
    merged.update(adapter_caps)
    return merged


def test_sanitize_log_value_strips_control_characters() -> None:
    from agent_app.observability import sanitize_log_value

    assert sanitize_log_value("device-1\r\ninjected=true") == "device-1\\r\\ninjected=true"
