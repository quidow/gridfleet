import asyncio

import pytest
from httpx2 import ASGITransport, AsyncClient

from agent_app.main import app
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import DiscoveryCandidate, FieldError, NormalizedDevice
from agent_app.pack.discovery import (
    _candidate_matches_platform,
    _platform_family_matches,
    enumerate_pack_candidates,
    pack_device_properties,
)
from agent_app.pack.manifest import AppiumInstallable, DesiredPack, DesiredPlatform
from tests.pack.fake_worker import FakeWorkerHandle


@pytest.mark.asyncio
async def test_pack_devices_endpoint_returns_empty_without_desired_packs() -> None:
    """Without desired packs (no pack state loop), the endpoint returns empty candidates."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/agent/pack/devices")
    assert resp.status_code == 200
    body = resp.json()
    assert body["candidates"] == []
    assert body["complete_gather"] is False


# ── enumerate_pack_candidates unit tests ────────────────────────────


def _pack(
    pack_id: str = "appium-uiautomator2",
    release: str = "1.0",
    platforms: list[DesiredPlatform] | None = None,
) -> DesiredPack:
    return DesiredPack(
        id=pack_id,
        release=release,
        appium_server=AppiumInstallable("npm", "appium", "2.11.5", None, []),
        appium_driver=AppiumInstallable("npm", "appium-uiautomator2-driver", "3.6.0", None, []),
        platforms=platforms or [],
    )


def _platform(
    platform_id: str = "android_mobile",
    device_types: list[str] | None = None,
    connection_types: list[str] | None = None,
) -> DesiredPlatform:
    return DesiredPlatform(
        id=platform_id,
        automation_name="UiAutomator2",
        device_types=device_types or ["real_device"],
        connection_types=connection_types or ["usb"],
        identity_scheme="android_serial",
        identity_scope="host",
        stereotype={},
        appium_platform_name="Android",
    )


@pytest.mark.asyncio
async def test_enumerate_skips_duplicate_pack_releases() -> None:
    registry = AdapterRegistry()
    pack = _pack()
    result = await enumerate_pack_candidates([pack, pack], adapter_registry=registry, host_id="h1")
    assert result == {"candidates": [], "complete_gather": False}


@pytest.mark.asyncio
async def test_enumerate_without_desired_packs_is_incomplete() -> None:
    result = await enumerate_pack_candidates([], adapter_registry=AdapterRegistry(), host_id="h1")

    assert result == {"candidates": [], "complete_gather": False}


class _PackScopeAdapter:
    pack_id = "appium-uiautomator2"
    pack_release = "1.0"
    discovery_scope = "pack"

    async def discover(self, ctx: object) -> list[DiscoveryCandidate]:
        return [
            DiscoveryCandidate(
                identity_scheme="android_serial",
                identity_value="serial1",
                suggested_name="Pixel",
                detected_properties={"device_type": "real_device", "connection_type": "usb"},
                runnable=True,
                missing_requirements=[],
                field_errors=[],
            )
        ]


@pytest.mark.asyncio
async def test_enumerate_pack_scope_adapter() -> None:
    registry = AdapterRegistry()
    adapter = _PackScopeAdapter()
    registry.set("appium-uiautomator2", "1.0", FakeWorkerHandle(adapter))  # type: ignore[arg-type]
    pack = _pack(platforms=[_platform()])
    result = await enumerate_pack_candidates([pack], adapter_registry=registry, host_id="h1")
    assert result["complete_gather"] is True
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["pack_id"] == "appium-uiautomator2"
    assert result["candidates"][0]["identity_value"] == "serial1"


class _FailingAdapter:
    pack_id = "appium-uiautomator2"
    pack_release = "1.0"

    async def discover(self, ctx: object) -> list[DiscoveryCandidate]:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_enumerate_handles_adapter_exception_pack_scope() -> None:
    registry = AdapterRegistry()
    adapter = _FailingAdapter()
    registry.set("appium-uiautomator2", "1.0", FakeWorkerHandle(adapter))  # type: ignore[arg-type]
    pack = _pack(platforms=[_platform()])
    adapter.discovery_scope = "pack"
    result = await enumerate_pack_candidates([pack], adapter_registry=registry, host_id="h1")
    assert result == {"candidates": [], "complete_gather": False}


@pytest.mark.asyncio
async def test_enumerate_handles_adapter_exception_per_platform() -> None:
    registry = AdapterRegistry()
    adapter = _FailingAdapter()
    registry.set("appium-uiautomator2", "1.0", FakeWorkerHandle(adapter))  # type: ignore[arg-type]
    pack = _pack(platforms=[_platform()])
    result = await enumerate_pack_candidates([pack], adapter_registry=registry, host_id="h1")
    assert result == {"candidates": [], "complete_gather": False}


# ── _candidate_matches_platform tests ──────────────────────────────


def test_candidate_matches_platform_props_not_dict() -> None:
    raw = DiscoveryCandidate(
        identity_scheme="android_serial",
        identity_value="s1",
        suggested_name="Pixel",
        detected_properties="not-a-dict",  # type: ignore[arg-type]
        runnable=True,
        missing_requirements=[],
        field_errors=[],
    )
    platform = _platform()
    assert _candidate_matches_platform(raw, platform) is True


def test_candidate_matches_platform_connection_type_filter() -> None:
    raw = DiscoveryCandidate(
        identity_scheme="android_serial",
        identity_value="s1",
        suggested_name="Pixel",
        detected_properties={"device_type": "real_device", "connection_type": "wifi"},
        runnable=True,
        missing_requirements=[],
        field_errors=[],
    )
    platform = _platform(connection_types=["usb"])
    assert _candidate_matches_platform(raw, platform) is False


def test_platform_family_matches_exact() -> None:
    assert _platform_family_matches("android_mobile", "android_mobile") is True


def test_platform_family_matches_prefix() -> None:
    assert _platform_family_matches("android", "android_mobile") is True


def test_platform_family_matches_no_match() -> None:
    assert _platform_family_matches("ios", "android_mobile") is False


def test_platform_family_matches_case_insensitive() -> None:
    assert _platform_family_matches("Android_Mobile", "android_mobile") is True


def test_platform_family_matches_hyphen_to_underscore() -> None:
    assert _platform_family_matches("android-mobile", "android_mobile") is True


# ── pack_device_properties unit tests ──────────────────────────────


class _NormalizeAdapter:
    pack_id = "appium-uiautomator2"
    pack_release = "1.0"

    async def normalize_device(self, ctx: object) -> NormalizedDevice:
        return NormalizedDevice(
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="serial1",
            connection_target="adb:serial1",
            ip_address="10.0.0.1",
            device_type="real_device",
            connection_type="usb",
            os_version="15",
            field_errors=[],
            manufacturer="Google",
            model="Pixel",
        )


class _FailingNormalizeAdapter:
    pack_id = "appium-uiautomator2"
    pack_release = "1.0"

    async def normalize_device(self, ctx: object) -> NormalizedDevice:
        raise RuntimeError("normalize failed")


class _FieldErrorNormalizeAdapter:
    pack_id = "appium-uiautomator2"
    pack_release = "1.0"

    async def normalize_device(self, ctx: object) -> NormalizedDevice:
        return NormalizedDevice(
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="serial1",
            connection_target="adb:serial1",
            ip_address="10.0.0.1",
            device_type="real_device",
            connection_type="usb",
            os_version="15",
            field_errors=[FieldError("identity", "bad")],
        )


@pytest.mark.asyncio
async def test_pack_device_properties_pack_id_mismatch_in_candidates() -> None:
    registry = AdapterRegistry()
    adapter = _PackScopeAdapter()
    registry.set("appium-uiautomator2", "1.0", FakeWorkerHandle(adapter))  # type: ignore[arg-type]
    pack = _pack(platforms=[_platform()])
    result = await pack_device_properties(
        "serial1",
        "other-pack",
        [pack],
        adapter_registry=registry,
        host_id="h1",
    )
    assert result is None


@pytest.mark.asyncio
async def test_pack_device_properties_pack_id_mismatch_in_fallback() -> None:
    registry = AdapterRegistry()
    adapter = _NormalizeAdapter()
    registry.set("appium-uiautomator2", "1.0", FakeWorkerHandle(adapter))  # type: ignore[arg-type]
    pack = _pack(platforms=[_platform()])
    result = await pack_device_properties(
        "serial1",
        "other-pack",
        [pack],
        adapter_registry=registry,
        host_id="h1",
    )
    assert result is None


@pytest.mark.asyncio
async def test_pack_device_properties_adapter_none() -> None:
    registry = AdapterRegistry()
    pack = _pack(platforms=[_platform()])
    result = await pack_device_properties(
        "serial1",
        "appium-uiautomator2",
        [pack],
        adapter_registry=registry,
        host_id="h1",
    )
    assert result is None


@pytest.mark.asyncio
async def test_pack_device_properties_normalize_exception_continues() -> None:
    """When normalize_device raises on one platform, it should try the next."""
    registry = AdapterRegistry()
    adapter = _FailingNormalizeAdapter()
    registry.set("appium-uiautomator2", "1.0", FakeWorkerHandle(adapter))  # type: ignore[arg-type]
    pack = _pack(platforms=[_platform(), _platform(platform_id="android_tablet")])
    result = await pack_device_properties(
        "serial1",
        "appium-uiautomator2",
        [pack],
        adapter_registry=registry,
        host_id="h1",
    )
    assert result is None


@pytest.mark.asyncio
async def test_pack_device_properties_field_errors_skips() -> None:
    registry = AdapterRegistry()
    adapter = _FieldErrorNormalizeAdapter()
    registry.set("appium-uiautomator2", "1.0", FakeWorkerHandle(adapter))  # type: ignore[arg-type]
    pack = _pack(platforms=[_platform()])
    result = await pack_device_properties(
        "serial1",
        "appium-uiautomator2",
        [pack],
        adapter_registry=registry,
        host_id="h1",
    )
    assert result is None


@pytest.mark.asyncio
async def test_pack_device_properties_successful_fallback() -> None:
    registry = AdapterRegistry()
    adapter = _NormalizeAdapter()
    registry.set("appium-uiautomator2", "1.0", FakeWorkerHandle(adapter))  # type: ignore[arg-type]
    pack = _pack(platforms=[_platform()])
    result = await pack_device_properties(
        "serial1",
        "appium-uiautomator2",
        [pack],
        adapter_registry=registry,
        host_id="h1",
    )
    assert result is not None
    assert result["pack_id"] == "appium-uiautomator2"
    assert result["platform_id"] == "android_mobile"
    assert result["identity_value"] == "serial1"
    assert result["detected_properties"]["manufacturer"] == "Google"
    assert result["detected_properties"]["model"] == "Pixel"
    assert result["runnable"] is True
    assert result["field_errors"] == []


class _MovedDeviceAdapter:
    """Direct query answers with the WRONG device (a stranger took the old IP);
    discovery finds the expected serial at its new address."""

    pack_id = "appium-uiautomator2"
    pack_release = "1.0"
    discovery_scope = "pack"

    async def normalize_device(self, ctx: object) -> NormalizedDevice:
        return NormalizedDevice(
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="stranger-serial",
            connection_target="10.0.0.5",
            ip_address="10.0.0.5",
            device_type="real_device",
            connection_type="usb",
            os_version="15",
            field_errors=[],
        )

    async def discover(self, ctx: object) -> list[DiscoveryCandidate]:
        return [
            DiscoveryCandidate(
                identity_scheme="android_serial",
                identity_value="expected-serial",
                suggested_name="Moved Device",
                detected_properties={
                    "connection_target": "10.0.0.9",
                    "ip_address": "10.0.0.9",
                    "device_type": "real_device",
                    "connection_type": "usb",
                },
                runnable=True,
                missing_requirements=[],
                field_errors=[],
            )
        ]


@pytest.mark.asyncio
async def test_pack_device_properties_direct_hit_skips_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = AdapterRegistry()
    registry.set("appium-uiautomator2", "1.0", FakeWorkerHandle(_NormalizeAdapter()))  # type: ignore[arg-type]

    async def explode(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("sweep must not run when the direct query answers")

    monkeypatch.setattr("agent_app.pack.discovery.enumerate_pack_candidates", explode)
    result = await pack_device_properties(
        "serial1",
        "appium-uiautomator2",
        [_pack(platforms=[_platform()])],
        adapter_registry=registry,
        host_id="h1",
    )
    assert result is not None
    assert result["identity_value"] == "serial1"


@pytest.mark.asyncio
async def test_pack_device_properties_direct_hit_with_matching_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = AdapterRegistry()
    registry.set("appium-uiautomator2", "1.0", FakeWorkerHandle(_NormalizeAdapter()))  # type: ignore[arg-type]

    async def explode(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("sweep must not run when identity matches the direct answer")

    monkeypatch.setattr("agent_app.pack.discovery.enumerate_pack_candidates", explode)
    result = await pack_device_properties(
        "serial1",
        "appium-uiautomator2",
        [_pack(platforms=[_platform()])],
        adapter_registry=registry,
        host_id="h1",
        identity_value="serial1",
    )
    assert result is not None
    assert result["identity_value"] == "serial1"


@pytest.mark.asyncio
async def test_pack_device_properties_identity_mismatch_falls_back_to_sweep() -> None:
    registry = AdapterRegistry()
    registry.set("appium-uiautomator2", "1.0", FakeWorkerHandle(_MovedDeviceAdapter()))  # type: ignore[arg-type]
    result = await pack_device_properties(
        "10.0.0.5",
        "appium-uiautomator2",
        [_pack(platforms=[_platform()])],
        adapter_registry=registry,
        host_id="h1",
        identity_value="expected-serial",
    )
    assert result is not None
    assert result["identity_value"] == "expected-serial"
    assert result["detected_properties"]["connection_target"] == "10.0.0.9"


@pytest.mark.asyncio
async def test_pack_device_properties_identity_not_found_anywhere() -> None:
    registry = AdapterRegistry()
    registry.set("appium-uiautomator2", "1.0", FakeWorkerHandle(_MovedDeviceAdapter()))  # type: ignore[arg-type]
    result = await pack_device_properties(
        "10.0.0.5",
        "appium-uiautomator2",
        [_pack(platforms=[_platform()])],
        adapter_registry=registry,
        host_id="h1",
        identity_value="ghost-serial",
    )
    assert result is None


@pytest.mark.asyncio
async def test_concurrent_sweep_fallbacks_share_one_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def counting_enumerate(*args: object, **kwargs: object) -> dict[str, object]:
        calls["n"] += 1
        await asyncio.sleep(0)
        return {"candidates": []}

    monkeypatch.setattr("agent_app.pack.discovery.enumerate_pack_candidates", counting_enumerate)
    registry = AdapterRegistry()  # no adapters -> direct path misses, sweep fallback runs
    pack = _pack(platforms=[_platform()])
    results = await asyncio.gather(
        *(
            pack_device_properties(
                f"10.0.0.{i}",
                "appium-uiautomator2",
                [pack],
                adapter_registry=registry,
                host_id="h1",
            )
            for i in range(5)
        )
    )
    assert all(r is None for r in results)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_intake_enumeration_route_bypasses_sweep_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def counting_enumerate(*args: object, **kwargs: object) -> dict[str, object]:
        calls["n"] += 1
        return {"candidates": []}

    monkeypatch.setattr("agent_app.pack.router.enumerate_pack_candidates", counting_enumerate)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.get("/agent/pack/devices")
        await client.get("/agent/pack/devices")
    assert calls["n"] == 2  # operator scans stay live, never cached
