from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_app.pack.runtime_types import AppiumInstallable


@dataclass
class DesiredPlatform:
    id: str
    automation_name: str
    device_types: list[str]
    connection_types: list[str]
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


@dataclass(frozen=True)
class ToolDependency:
    name: str
    description: str


@dataclass(frozen=True)
class RuntimePackage:
    package: str
    version: str


@dataclass
class DesiredPack:
    id: str
    release: str
    appium_server: AppiumInstallable
    appium_driver: AppiumInstallable
    platforms: list[DesiredPlatform]
    tarball_sha256: str | None = None
    tool_dependencies: list[ToolDependency] = field(default_factory=list)
    runtime_packages: list[RuntimePackage] = field(default_factory=list)

    @property
    def has_adapter_platform(self) -> bool:
        """True when the pack has platforms that can be served by its adapter."""

        return bool(self.platforms)


@dataclass
class DesiredPayload:
    host_id: str
    packs: list[DesiredPack]


def parse_desired_payload(payload: dict[str, Any]) -> DesiredPayload:
    packs: list[DesiredPack] = []
    for raw in payload.get("packs", []):
        requires = raw.get("requires") or {}
        tool_deps = [
            ToolDependency(name=td["name"], description=td["description"])
            for td in (requires.get("tool_dependencies") or [])
        ]
        packs.append(
            DesiredPack(
                id=raw["id"],
                release=raw["release"],
                appium_server=_installable(raw["appium_server"]),
                appium_driver=_installable(raw["appium_driver"]),
                platforms=[_platform(p) for p in raw["platforms"]],
                tarball_sha256=raw.get("tarball_sha256"),
                tool_dependencies=tool_deps,
                runtime_packages=[
                    RuntimePackage(package=rp["package"], version=rp["version"])
                    for rp in (raw.get("runtime_packages") or [])
                ],
            )
        )
    return DesiredPayload(
        host_id=payload["host_id"],
        packs=packs,
    )


def _installable(raw: dict[str, Any]) -> AppiumInstallable:
    return AppiumInstallable(
        source=raw["source"],
        package=raw["package"],
        version=raw["version"],
        recommended=raw.get("recommended"),
        known_bad=list(raw.get("known_bad") or []),
        github_repo=raw.get("github_repo"),
    )


def _platform(raw: dict[str, Any]) -> DesiredPlatform:
    return DesiredPlatform(
        id=raw["id"],
        automation_name=raw["automation_name"],
        appium_platform_name=raw.get("appium_platform_name", ""),
        device_types=list(raw["device_types"]),
        connection_types=list(raw["connection_types"]),
        identity_scheme=raw["identity"]["scheme"],
        identity_scope=raw["identity"]["scope"],
        stereotype=raw["capabilities"].get("stereotype", {}),
        lifecycle_actions=list(raw.get("lifecycle_actions") or []),
        connection_behavior=dict(raw.get("connection_behavior") or {}),
        device_type_overrides=dict(raw.get("device_type_overrides") or {}),
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
