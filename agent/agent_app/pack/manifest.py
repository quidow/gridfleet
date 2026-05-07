from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, cast

from agent_app.pack.runtime_types import AppiumInstallable, RuntimePolicy


@dataclass
class DesiredPlatform:
    id: str
    automation_name: str
    device_types: list[str]
    connection_types: list[str]
    grid_slots: list[str]
    identity_scheme: str
    identity_scope: str
    stereotype: dict[str, Any]
    appium_platform_name: str = ""
    lifecycle_actions: list[dict[str, Any]] = field(default_factory=list)
    connection_behavior: dict[str, Any] = field(default_factory=dict)
    device_type_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    def identity_for_device_type(self, device_type: str | None) -> tuple[str, str]:
        override = self.device_type_overrides.get(device_type or "")
        identity = override.get("identity") if isinstance(override, dict) else None
        if isinstance(identity, dict):
            return (
                str(identity.get("scheme") or self.identity_scheme),
                str(identity.get("scope") or self.identity_scope),
            )
        return self.identity_scheme, self.identity_scope


@dataclass
class DesiredFeature:
    id: str
    sidecar: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DesiredPack:
    id: str
    release: str
    appium_server: AppiumInstallable
    appium_driver: AppiumInstallable
    platforms: list[DesiredPlatform]
    features: list[DesiredFeature] = field(default_factory=list)
    runtime_policy: RuntimePolicy = field(default_factory=RuntimePolicy)
    tarball_sha256: str | None = None

    @property
    def has_adapter_platform(self) -> bool:
        """True when the pack has platforms that can be served by its adapter."""

        return bool(self.platforms)

    @property
    def sidecar_feature_ids(self) -> list[str]:
        return [feature.id for feature in self.features if feature.sidecar is not None]


@dataclass
class DesiredPlugin:
    name: str
    version: str
    source: str
    package: str | None


@dataclass
class DesiredPayload:
    host_id: str
    packs: list[DesiredPack]
    plugins: list[DesiredPlugin]


def parse_desired_payload(payload: dict[str, Any]) -> DesiredPayload:
    packs: list[DesiredPack] = []
    for raw in payload.get("packs", []):
        packs.append(
            DesiredPack(
                id=raw["id"],
                release=raw["release"],
                appium_server=_installable(raw["appium_server"]),
                appium_driver=_installable(raw["appium_driver"]),
                platforms=[_platform(p) for p in raw["platforms"]],
                features=_features(raw.get("features") or {}),
                runtime_policy=_runtime_policy(raw.get("runtime_policy") or {"strategy": "recommended"}),
                tarball_sha256=raw.get("tarball_sha256"),
            )
        )
    return DesiredPayload(
        host_id=payload["host_id"],
        packs=packs,
        plugins=[_plugin(p) for p in payload.get("plugins", [])],
    )


def _installable(raw: dict[str, Any]) -> AppiumInstallable:
    return AppiumInstallable(
        source=raw["source"],
        package=raw["package"],
        version=raw["version"],
        recommended=raw.get("recommended"),
        known_bad=list(raw.get("known_bad") or []),
        github_repo=raw.get("github_repo"),
        available_versions=[item for item in raw.get("available_versions", []) if isinstance(item, str)],
    )


def _platform(raw: dict[str, Any]) -> DesiredPlatform:
    return DesiredPlatform(
        id=raw["id"],
        automation_name=raw["automation_name"],
        appium_platform_name=raw.get("appium_platform_name", ""),
        device_types=list(raw["device_types"]),
        connection_types=list(raw["connection_types"]),
        grid_slots=list(raw["grid_slots"]),
        identity_scheme=raw["identity"]["scheme"],
        identity_scope=raw["identity"]["scope"],
        stereotype=raw["capabilities"].get("stereotype", {}),
        lifecycle_actions=list(raw.get("lifecycle_actions") or []),
        connection_behavior=dict(raw.get("connection_behavior") or {}),
        device_type_overrides=dict(raw.get("device_type_overrides") or {}),
    )


def _runtime_policy(raw: dict[str, Any]) -> RuntimePolicy:
    return RuntimePolicy(
        strategy=cast("Literal['recommended', 'latest_patch', 'exact']", raw.get("strategy", "recommended")),
        appium_server_version=raw.get("appium_server_version"),
        appium_driver_version=raw.get("appium_driver_version"),
    )


def resolve_desired_platform(
    desired_packs: list[DesiredPack],
    *,
    pack_id: str,
    platform_id: str,
) -> DesiredPlatform | None:
    for pack in desired_packs:
        if pack.id != pack_id:
            continue
        for platform in pack.platforms:
            if platform.id == platform_id:
                return platform
    return None


def _features(raw: dict[str, Any]) -> list[DesiredFeature]:
    return [
        DesiredFeature(
            id=feature_id,
            sidecar=dict(feature_data.get("sidecar") or {}) or None,
            actions=list(feature_data.get("actions") or []),
        )
        for feature_id, feature_data in sorted(raw.items())
        if isinstance(feature_data, dict)
    ]


def _plugin(raw: dict[str, Any]) -> DesiredPlugin:
    return DesiredPlugin(
        name=raw["name"],
        version=raw["version"],
        source=raw["source"],
        package=raw.get("package"),
    )
