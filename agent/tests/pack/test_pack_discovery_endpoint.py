from typing import ClassVar

import pytest
from httpx import ASGITransport, AsyncClient

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


@pytest.mark.asyncio
async def test_pack_device_properties_endpoint_uses_latest_desired_packs(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_app.pack.manifest import AppiumInstallable, DesiredPack, DesiredPlatform

    desired = DesiredPack(
        id="appium-uiautomator2",
        release="1.0",
        appium_server=AppiumInstallable("npm", "appium", "2.11.5", None, []),
        appium_driver=AppiumInstallable("npm", "appium-uiautomator2-driver", "3.6.0", None, []),
        platforms=[
            DesiredPlatform(
                id="android_mobile",
                automation_name="UiAutomator2",
                device_types=["real_device"],
                connection_types=["usb"],
                grid_slots=["native"],
                identity_scheme="android_serial",
                identity_scope="host",
                stereotype={},
                appium_platform_name="Android",
            )
        ],
    )

    class Loop:
        latest_desired_packs: ClassVar[list[DesiredPack]] = [desired]

    async def fake_enumerate(*args: object, **kwargs: object) -> dict[str, object]:
        assert args[0] == [desired]
        return {
            "candidates": [
                {
                    "pack_id": "appium-uiautomator2",
                    "platform_id": "android_mobile",
                    "identity_scheme": "android_serial",
                    "identity_scope": "host",
                    "identity_value": "stable-serial",
                    "suggested_name": "Pixel",
                    "detected_properties": {
                        "connection_target": "adb-transport",
                        "os_version": "15",
                        "ip_address": "10.0.0.25",
                    },
                    "runnable": True,
                    "missing_requirements": [],
                }
            ],
        }

    monkeypatch.setattr(app.state, "pack_state_loop", Loop(), raising=False)
    monkeypatch.setattr("agent_app.pack.discovery.enumerate_pack_candidates", fake_enumerate)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/agent/pack/devices/stable-serial/properties",
            params={"pack_id": "appium-uiautomator2"},
        )

    assert resp.status_code == 200
    assert resp.json()["detected_properties"]["os_version"] == "15"


@pytest.mark.asyncio
async def test_pack_device_properties_not_found() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/agent/pack/devices/NONEXISTENT/properties",
            params={"pack_id": "appium-uiautomator2"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pack_devices_endpoint_returns_empty_without_desired_packs() -> None:
    """Without desired packs (no pack state loop), the endpoint returns empty candidates."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/agent/pack/devices")
    assert resp.status_code == 200
    body = resp.json()
    assert body["candidates"] == []


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
        grid_slots=["native"],
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
    assert result == {"candidates": []}


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
                feature_status=[],
            )
        ]


@pytest.mark.asyncio
async def test_enumerate_pack_scope_adapter() -> None:
    registry = AdapterRegistry()
    adapter = _PackScopeAdapter()
    registry.set("appium-uiautomator2", "1.0", adapter)  # type: ignore[arg-type]
    pack = _pack(platforms=[_platform()])
    result = await enumerate_pack_candidates([pack], adapter_registry=registry, host_id="h1")
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
    registry.set("appium-uiautomator2", "1.0", adapter)  # type: ignore[arg-type]
    pack = _pack(platforms=[_platform()])
    adapter.discovery_scope = "pack"
    result = await enumerate_pack_candidates([pack], adapter_registry=registry, host_id="h1")
    assert result == {"candidates": []}


@pytest.mark.asyncio
async def test_enumerate_handles_adapter_exception_per_platform() -> None:
    registry = AdapterRegistry()
    adapter = _FailingAdapter()
    registry.set("appium-uiautomator2", "1.0", adapter)  # type: ignore[arg-type]
    pack = _pack(platforms=[_platform()])
    result = await enumerate_pack_candidates([pack], adapter_registry=registry, host_id="h1")
    assert result == {"candidates": []}


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
        feature_status=[],
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
        feature_status=[],
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
    registry.set("appium-uiautomator2", "1.0", adapter)  # type: ignore[arg-type]
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
    registry.set("appium-uiautomator2", "1.0", adapter)  # type: ignore[arg-type]
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
    registry.set("appium-uiautomator2", "1.0", adapter)  # type: ignore[arg-type]
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
    registry.set("appium-uiautomator2", "1.0", adapter)  # type: ignore[arg-type]
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
    registry.set("appium-uiautomator2", "1.0", adapter)  # type: ignore[arg-type]
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
