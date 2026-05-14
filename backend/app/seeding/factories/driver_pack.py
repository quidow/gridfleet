"""Driver-pack catalog factories for demo seed scenarios."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.packs import manifest as pack_manifest
from app.packs.models import (
    DriverPack,
    DriverPackFeature,
    DriverPackPlatform,
    DriverPackRelease,
    PackState,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _installable(package: str, version: str, recommended: str) -> dict[str, Any]:
    return {
        "source": "npm",
        "package": package,
        "version": version,
        "recommended": recommended,
        "known_bad": [],
    }


def _health_checks(labels: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"id": check_id, "label": label} for check_id, label in labels]


def _port_resources(*capability_names: str, derived_data_path: bool = False) -> dict[str, Any]:
    starts = {
        "appium:systemPort": 8200,
        "appium:chromedriverPort": 9515,
        "appium:mjpegServerPort": 9200,
        "appium:wdaLocalPort": 8100,
    }
    return {
        "ports": [{"capability_name": name, "start": starts[name]} for name in capability_names],
        "derived_data_path": derived_data_path,
    }


def _android_platform(
    platform_id: str,
    display_name: str,
    *,
    icon_kind: str,
    supports_emulator: bool = False,
) -> dict[str, Any]:
    device_types = ["real_device", "emulator"] if supports_emulator else ["real_device"]
    connection_types = ["usb", "network", "virtual"] if supports_emulator else ["usb", "network"]
    return {
        "id": platform_id,
        "display_name": display_name,
        "automation_name": "UiAutomator2",
        "appium_platform_name": "Android",
        "device_types": device_types,
        "connection_types": connection_types,
        "grid_slots": ["native", "chrome"],
        "capabilities": {
            "stereotype": {
                "appium:platformName": "Android",
                "appium:automationName": "UiAutomator2",
            },
            "session_required": [],
        },
        "identity": {"scheme": "android_serial", "scope": "host"},
        "display": {"icon_kind": icon_kind},
        "health_checks": _health_checks(
            [
                ("adb_connected", "ADB Connected"),
                ("adb_responsive", "ADB Responsive"),
                ("boot_completed", "Boot Completed"),
                ("ping", "IP Reachable"),
            ]
        ),
        "lifecycle_actions": [{"id": action} for action in ["state", "reconnect"]],
        "connection_behavior": {
            "default_device_type": "real_device",
            "default_connection_type": "usb",
            "requires_ip_address": False,
            "requires_connection_target": True,
            "allow_transport_identity_until_host_resolution": True,
            "host_resolution_action": "resolve",
        },
        "parallel_resources": _port_resources(
            "appium:systemPort",
            "appium:chromedriverPort",
            "appium:mjpegServerPort",
        ),
        "device_type_overrides": {
            "emulator": {
                "lifecycle_actions": [{"id": action} for action in ["state", "boot", "shutdown"]],
                "connection_behavior": {
                    "default_device_type": "emulator",
                    "default_connection_type": "virtual",
                    "requires_ip_address": False,
                    "requires_connection_target": True,
                },
            }
        }
        if supports_emulator
        else {},
    }


def _apple_platform(
    platform_id: str,
    display_name: str,
    *,
    appium_platform_name: str,
    icon_kind: str,
    supports_simulator: bool = False,
    real_default_capabilities: dict[str, Any] | None = None,
    real_device_fields_schema: list[dict[str, Any]] | None = None,
    requires_coredevice_tunnel: bool = True,
) -> dict[str, Any]:
    device_type_overrides: dict[str, Any] = {}
    if real_default_capabilities or real_device_fields_schema:
        device_type_overrides["real_device"] = {
            "default_capabilities": real_default_capabilities or {},
            "device_fields_schema": real_device_fields_schema or [],
            "connection_behavior": {
                "default_device_type": "real_device",
                "default_connection_type": "usb",
                "requires_ip_address": False,
                "requires_connection_target": True,
            },
        }
    if supports_simulator:
        device_type_overrides["simulator"] = {
            "identity": {"scheme": "simulator_udid", "scope": "host"},
            "lifecycle_actions": [{"id": action} for action in ["state", "boot", "shutdown"]],
            "default_capabilities": {},
            "connection_behavior": {
                "default_device_type": "simulator",
                "default_connection_type": "virtual",
                "requires_ip_address": False,
                "requires_connection_target": True,
            },
        }
    health_checks = [
        ("devicectl_visible", "devicectl Visible"),
        ("devicectl_paired", "Device Paired"),
        ("ios_booted", "OS Booted"),
        ("developer_mode", "Developer Mode"),
        ("simulator_booted", "Simulator Booted"),
        ("simulator_responsive", "Simulator Responsive"),
        ("ping", "IP Reachable"),
    ]
    if requires_coredevice_tunnel:
        health_checks.insert(2, ("devicectl_tunnel", "CoreDevice Tunnel"))
        health_checks.insert(5, ("ddi_services", "Developer Services"))
    return {
        "id": platform_id,
        "display_name": display_name,
        "automation_name": "XCUITest",
        "appium_platform_name": appium_platform_name,
        "device_types": ["real_device", "simulator"] if supports_simulator else ["real_device"],
        "connection_types": ["usb", "network", "virtual"] if supports_simulator else ["usb", "network"],
        "grid_slots": ["native"],
        "capabilities": {
            "stereotype": {
                "appium:platformName": appium_platform_name,
                "appium:automationName": "XCUITest",
            },
            "session_required": [],
        },
        "identity": {
            "scheme": "apple_udid",
            "scope": "global",
        },
        "device_fields_schema": [],
        "display": {"icon_kind": icon_kind},
        "health_checks": _health_checks(health_checks),
        "lifecycle_actions": [{"id": action} for action in ["state", "reconnect"]],
        "default_capabilities": {},
        "connection_behavior": {
            "default_device_type": "real_device",
            "default_connection_type": "usb",
            "requires_ip_address": False,
            "requires_connection_target": True,
        },
        "parallel_resources": _port_resources(
            "appium:wdaLocalPort",
            "appium:mjpegServerPort",
            derived_data_path=True,
        ),
        "device_type_overrides": device_type_overrides,
    }


def _ios_real_device_fields() -> list[dict[str, Any]]:
    return [
        {
            "id": "use_preinstalled_wda",
            "label": "Use pre-installed WDA",
            "type": "bool",
            "default": False,
            "capability_name": "appium:usePreinstalledWDA",
        },
        {
            "id": "updated_wda_bundle_id",
            "label": "Updated WDA bundle ID",
            "type": "string",
            "capability_name": "appium:updatedWDABundleId",
        },
        {
            "id": "updated_wda_bundle_id_suffix",
            "label": "Updated WDA bundle ID suffix",
            "type": "string",
            "capability_name": "appium:updatedWDABundleIdSuffix",
        },
        {
            "id": "prebuilt_wda_path",
            "label": "Prebuilt WDA path",
            "type": "path",
            "capability_name": "appium:prebuiltWDAPath",
        },
        {
            "id": "wda_launch_timeout",
            "label": "WDA launch timeout (ms)",
            "type": "int",
            "capability_name": "appium:wdaLaunchTimeout",
        },
        {
            "id": "xcode_org_id",
            "label": "Xcode team ID",
            "type": "string",
            "capability_name": "appium:xcodeOrgId",
        },
        {
            "id": "xcode_signing_id",
            "label": "Xcode signing ID",
            "type": "string",
            "default": "Apple Development",
            "capability_name": "appium:xcodeSigningId",
        },
        {
            "id": "xcode_config_file",
            "label": "Xcode config file",
            "type": "path",
            "capability_name": "appium:xcodeConfigFile",
        },
        {
            "id": "show_xcode_log",
            "label": "Show Xcode log",
            "type": "bool",
            "default": False,
            "capability_name": "appium:showXcodeLog",
        },
    ]


def _tvos_real_device_fields() -> list[dict[str, Any]]:
    return [
        {
            "id": "wda_base_url",
            "label": "WDA base URL",
            "type": "network_endpoint",
            "required_for_session": True,
            "capability_name": "appium:wdaBaseUrl",
        },
        {
            "id": "use_preinstalled_wda",
            "label": "Use pre-installed WDA",
            "type": "bool",
            "default": True,
            "capability_name": "appium:usePreinstalledWDA",
        },
        {
            "id": "updated_wda_bundle_id",
            "label": "Updated WDA bundle ID",
            "type": "string",
            "required_for_session": True,
            "capability_name": "appium:updatedWDABundleId",
        },
    ]


def _roku_platform() -> dict[str, Any]:
    return {
        "id": "roku_network",
        "display_name": "Roku (network)",
        "automation_name": "Roku",
        "appium_platform_name": "roku",
        "device_types": ["real_device"],
        "connection_types": ["network"],
        "grid_slots": ["native"],
        "capabilities": {
            "stereotype": {
                "appium:platformName": "roku",
                "appium:automationName": "Roku",
            },
            "session_required": ["appium:password"],
        },
        "default_capabilities": {"appium:ip": "{device.ip_address}"},
        "identity": {"scheme": "roku_serial", "scope": "global"},
        "device_fields_schema": [
            {
                "id": "roku_password",
                "label": "Developer password",
                "type": "string",
                "required_for_session": True,
                "sensitive": True,
                "capability_name": "appium:password",
            }
        ],
        "display": {"icon_kind": "set_top"},
        "health_checks": _health_checks(
            [
                ("ping", "IP Reachable"),
                ("ecp", "ECP Reachable"),
                ("developer_mode", "Developer Mode"),
            ]
        ),
        "lifecycle_actions": [],
        "connection_behavior": {
            "default_device_type": "real_device",
            "default_connection_type": "network",
            "requires_ip_address": True,
            "requires_connection_target": False,
        },
    }


def _demo_manifests() -> tuple[pack_manifest.Manifest, ...]:
    raw_manifests: tuple[dict[str, Any], ...] = (
        {
            "schema_version": 1,
            "id": "appium-uiautomator2",
            "release": "2026.04.0",
            "display_name": "Appium UiAutomator2",
            "maintainer": "gridfleet-team",
            "license": "Apache-2.0",
            "appium_server": _installable("appium", ">=2.5,<3", "2.11.5"),
            "appium_driver": _installable("appium-uiautomator2-driver", ">=3,<5", "3.6.0"),
            "doctor": [
                {"id": "adb", "description": "ADB binary is available and responsive"},
                {"id": "driver", "description": "UiAutomator2 driver package is installed"},
            ],
            "insecure_features": ["uiautomator2:chromedriver_autodownload"],
            "platforms": [
                _android_platform(
                    "android_mobile",
                    "Android",
                    icon_kind="mobile",
                    supports_emulator=True,
                ),
                _android_platform(
                    "android_tv",
                    "Android TV",
                    icon_kind="tv",
                    supports_emulator=True,
                ),
                _android_platform(
                    "firetv_real",
                    "Fire TV (real device)",
                    icon_kind="tv",
                ),
            ],
        },
        {
            "schema_version": 1,
            "id": "appium-roku-dlenroc",
            "release": "2026.04.0",
            "display_name": "Roku (dlenroc)",
            "maintainer": "community",
            "license": "MIT",
            "appium_server": _installable("appium", ">=2.5,<3", "2.11.5"),
            "appium_driver": {
                "source": "github",
                "github_repo": "dlenroc/appium-roku-driver#b34f49a8652d70f669cac7ec86805ed4378aaff8",
                "package": "@dlenroc/appium-roku-driver",
                "version": ">=0.11,<1",
                "recommended": "0.13.3",
                "known_bad": ["0.13.1"],
            },
            "doctor": [
                {"id": "ecp", "description": "Roku ECP endpoint is reachable"},
                {"id": "driver", "description": "Roku Appium driver package is installed"},
            ],
            "platforms": [_roku_platform()],
        },
        {
            "schema_version": 1,
            "id": "appium-xcuitest",
            "release": "2026.04.12",
            "display_name": "Appium XCUITest",
            "maintainer": "gridfleet-team",
            "license": "Apache-2.0",
            "appium_server": _installable("appium", ">=2.5,<3", "2.19.0"),
            "appium_driver": _installable("appium-xcuitest-driver", ">=7,<10", "9.3.1"),
            "doctor": [
                {"id": "xcode", "description": "Xcode command line tools are available"},
                {"id": "driver", "description": "XCUITest driver package is installed"},
            ],
            "workarounds": [
                {
                    "id": "tvos_devicectl_preference",
                    "applies_when": {"platform_ids": ["tvos"], "device_types": ["real_device"]},
                    "env": {"APPIUM_XCUITEST_PREFER_DEVICECTL": "1"},
                }
            ],
            "platforms": [
                _apple_platform(
                    "ios",
                    "iOS",
                    appium_platform_name="iOS",
                    icon_kind="mobile",
                    supports_simulator=True,
                    real_device_fields_schema=_ios_real_device_fields(),
                ),
                _apple_platform(
                    "tvos",
                    "tvOS",
                    appium_platform_name="tvOS",
                    icon_kind="tv",
                    supports_simulator=True,
                    real_default_capabilities={
                        "appium:platformVersion": "{device.os_version}",
                        "appium:usePreinstalledWDA": True,
                    },
                    real_device_fields_schema=_tvos_real_device_fields(),
                    requires_coredevice_tunnel=False,
                ),
            ],
        },
    )
    return tuple(pack_manifest.Manifest.model_validate(raw) for raw in raw_manifests)


async def seed_demo_driver_packs(session: AsyncSession) -> None:
    """Seed the driver-pack rows used by the full demo fleet."""
    for manifest in _demo_manifests():
        pack = DriverPack(
            id=manifest.id,
            origin="uploaded",
            display_name=manifest.display_name,
            maintainer=manifest.maintainer,
            license=manifest.license,
            current_release=manifest.release,
            state=PackState.enabled,
            runtime_policy={"strategy": "recommended"},
        )
        session.add(pack)
        await session.flush()

        manifest_json = manifest.model_dump(mode="json")
        release = DriverPackRelease(
            pack_id=manifest.id,
            release=manifest.release,
            manifest_json=manifest_json,
            derived_from_pack_id=manifest.derived_from.pack_id if manifest.derived_from else None,
            derived_from_release=manifest.derived_from.release if manifest.derived_from else None,
            template_id=manifest.template_id,
        )
        session.add(release)
        await session.flush()

        for platform in manifest.platforms:
            session.add(
                DriverPackPlatform(
                    pack_release_id=release.id,
                    manifest_platform_id=platform.id,
                    display_name=platform.display_name,
                    automation_name=platform.automation_name,
                    appium_platform_name=platform.appium_platform_name,
                    device_types=list(platform.device_types),
                    connection_types=list(platform.connection_types),
                    grid_slots=list(platform.grid_slots),
                    data=platform.model_dump(mode="json"),
                )
            )

        for feature_id, feature in manifest.features.items():
            session.add(
                DriverPackFeature(
                    pack_release_id=release.id,
                    manifest_feature_id=feature_id,
                    data=feature.model_dump(mode="json"),
                )
            )

    await session.flush()
