from __future__ import annotations

import uuid  # noqa: TC003
from datetime import datetime  # noqa: TC003
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RuntimePolicy(BaseModel):
    strategy: Literal["recommended", "latest_patch", "exact"] = "recommended"
    appium_server_version: str | None = None
    appium_driver_version: str | None = None

    @model_validator(mode="after")
    def validate_exact_pins(self) -> RuntimePolicy:
        if self.strategy == "exact":
            missing = [
                name
                for name, value in (
                    ("appium_server_version", self.appium_server_version),
                    ("appium_driver_version", self.appium_driver_version),
                )
                if not value
            ]
            if missing:
                joined = ", ".join(missing)
                raise ValueError(f"exact runtime policy requires {joined}")
        elif self.appium_server_version is not None or self.appium_driver_version is not None:
            raise ValueError("version pins are only valid for exact runtime policy")
        return self


class RuntimePolicyPatch(BaseModel):
    runtime_policy: RuntimePolicy


class CurrentReleasePatch(BaseModel):
    release: str


class DerivedFrom(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pack_id: str
    release: str


class PlatformOut(BaseModel):
    id: str
    display_name: str
    automation_name: str
    appium_platform_name: str
    device_types: list[str]
    connection_types: list[str]
    grid_slots: list[str]
    identity_scheme: str
    identity_scope: str
    lifecycle_actions: list[dict[str, Any]] = Field(default_factory=list)
    health_checks: list[dict[str, Any]] = Field(default_factory=list)
    device_fields_schema: list[dict[str, Any]]
    capabilities: dict[str, Any]
    display_metadata: dict[str, Any] = Field(default_factory=dict)
    default_capabilities: dict[str, Any] = Field(default_factory=dict)
    connection_behavior: dict[str, Any] = Field(default_factory=dict)
    parallel_resources: dict[str, Any] = Field(default_factory=dict)
    device_type_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


class AppiumInstallableOut(BaseModel):
    source: str
    package: str
    version: str
    recommended: str | None = None
    known_bad: list[str] = Field(default_factory=list)
    github_repo: str | None = None


class ManifestWorkaroundOut(BaseModel):
    id: str
    applies_when: dict[str, Any] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)


class ManifestDoctorCheckOut(BaseModel):
    id: str
    description: str
    adapter_hook: str | None = None


class FeatureActionOut(BaseModel):
    id: str
    label: str


class FeatureOut(BaseModel):
    display_name: str
    description_md: str = ""
    actions: list[FeatureActionOut] = Field(default_factory=list)


class PackRuntimeSummaryOut(BaseModel):
    installed_hosts: int = 0
    blocked_hosts: int = 0
    actual_appium_server_versions: list[str] = Field(default_factory=list)
    actual_appium_driver_versions: list[str] = Field(default_factory=list)
    driver_drift_hosts: int = 0


class PackOut(BaseModel):
    id: str
    display_name: str
    maintainer: str = ""
    license: str = ""
    state: str
    current_release: str | None
    platforms: list[PlatformOut] = Field(default_factory=list)
    appium_server: AppiumInstallableOut | None = None
    appium_driver: AppiumInstallableOut | None = None
    workarounds: list[ManifestWorkaroundOut] = Field(default_factory=list)
    doctor: list[ManifestDoctorCheckOut] = Field(default_factory=list)
    insecure_features: list[str] = Field(default_factory=list)
    features: dict[str, FeatureOut] = Field(default_factory=dict)
    runtime_policy: RuntimePolicy = Field(default_factory=RuntimePolicy)
    active_runs: int = 0
    live_sessions: int = 0
    derived_from: DerivedFrom | None = None
    runtime_summary: PackRuntimeSummaryOut = Field(default_factory=PackRuntimeSummaryOut)


class PackCatalog(BaseModel):
    packs: list[PackOut]


class PackPlatforms(BaseModel):
    pack_id: str
    release: str
    platforms: list[PlatformOut]


class PackReleaseOut(BaseModel):
    release: str
    is_current: bool
    artifact_sha256: str | None
    created_at: datetime
    platform_ids: list[str] = Field(default_factory=list)


class PackReleasesOut(BaseModel):
    pack_id: str
    releases: list[PackReleaseOut]


class PackPatch(BaseModel):
    state: str


class HostRuntimeStatusOut(BaseModel):
    runtime_id: str
    appium_server_package: str
    appium_server_version: str
    driver_specs: list[dict[str, Any]]
    plugin_specs: list[dict[str, Any]]
    appium_home: str | None
    status: str
    blocked_reason: str | None
    plugins: list[dict[str, Any]] = Field(default_factory=list)


class HostPackDoctorOut(BaseModel):
    pack_id: str
    check_id: str
    ok: bool
    message: str


class HostPackFeatureStatusOut(BaseModel):
    pack_id: str
    feature_id: str
    ok: bool
    detail: str


class HostPackStatusOut(BaseModel):
    pack_id: str
    pack_release: str
    runtime_id: str | None
    status: str
    resolved_install_spec: dict[str, Any] | None
    installer_log_excerpt: str | None
    resolver_version: str | None
    blocked_reason: str | None
    installed_at: datetime | None
    desired_appium_driver_version: str | None = None
    installed_appium_driver_version: str | None = None
    appium_driver_drift: bool = False


class HostDriverPacksOut(BaseModel):
    host_id: uuid.UUID
    packs: list[HostPackStatusOut]
    runtimes: list[HostRuntimeStatusOut]
    doctor: list[HostPackDoctorOut]
    features: list[HostPackFeatureStatusOut] = Field(default_factory=list)


class DriverPackHostDoctorOut(BaseModel):
    check_id: str
    ok: bool
    message: str


class DriverPackHostStatusOut(BaseModel):
    host_id: uuid.UUID
    hostname: str
    status: str
    pack_release: str
    runtime_id: str | None
    pack_status: str
    resolved_install_spec: dict[str, Any] | None
    installer_log_excerpt: str | None
    resolver_version: str | None
    blocked_reason: str | None
    installed_at: datetime | None
    desired_appium_driver_version: str | None = None
    installed_appium_driver_version: str | None = None
    appium_driver_drift: bool = False
    appium_home: str | None = None
    runtime_status: str | None = None
    runtime_blocked_reason: str | None = None
    appium_server_version: str | None = None
    doctor: list[DriverPackHostDoctorOut] = Field(default_factory=list)


class DriverPackHostsOut(BaseModel):
    pack_id: str
    hosts: list[DriverPackHostStatusOut] = Field(default_factory=list)
