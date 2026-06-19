import re
from typing import Any, Literal

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ALLOWED_TEMPLATE_VARS: frozenset[str] = frozenset(
    {
        "device.ip_address",
        "device.connection_target",
        "device.identity_value",
        "device.os_version",
    }
)
_TEMPLATE_VAR_RE = re.compile(r"\{([^{}]+)\}")
_GITHUB_REPO_RE = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+(?:#[a-zA-Z0-9_.:/-]+)?$")


class ManifestValidationError(ValueError):
    """Raised when manifest YAML or schema validation fails."""

    pass


class AppiumInstallable(BaseModel):
    """Configuration for Appium server or driver installation source and version constraints."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["npm", "github", "local"]
    package: str
    version: str
    recommended: str | None = None
    known_bad: list[str] = Field(default_factory=list)
    github_repo: str | None = None

    @model_validator(mode="after")
    def _recommended_satisfies_version(self) -> "AppiumInstallable":
        """Ensure ``recommended`` (when set) satisfies the ``version`` range."""
        if self.recommended is None:
            return self
        try:
            specifier = SpecifierSet(self.version)
        except InvalidSpecifier as exc:
            raise ValueError(f"invalid version specifier {self.version!r}: {exc}") from exc
        try:
            recommended_version = Version(self.recommended)
        except InvalidVersion as exc:
            raise ValueError(f"invalid recommended version {self.recommended!r}: {exc}") from exc
        if recommended_version not in specifier:
            raise ValueError(
                f"recommended version {self.recommended!r} does not satisfy version range {self.version!r} "
                f"for package {self.package!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_github_repo(self) -> "AppiumInstallable":
        if self.source == "github" and not self.github_repo:
            raise ValueError("github_repo is required when source is 'github'")
        if self.source == "npm" and self.github_repo is not None:
            raise ValueError("github_repo must be None when source is 'npm'")
        if self.github_repo and not _GITHUB_REPO_RE.match(self.github_repo):
            raise ValueError(f"github_repo must be 'owner/repo' or 'owner/repo#ref' format, got: {self.github_repo}")
        return self


class Capabilities(BaseModel):
    """Capabilities stereotypes and session requirements for a platform."""

    model_config = ConfigDict(extra="forbid")

    stereotype: dict[str, Any] = {}
    session_required: list[str] = []


class Identity(BaseModel):
    """Device identity scheme and scope for a platform."""

    model_config = ConfigDict(extra="forbid")

    scheme: str
    scope: Literal["global", "host"]


class FieldSchema(BaseModel):
    """A setup field required by a platform for discovery, sessions, or custom readiness gates."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    type: Literal["string", "int", "bool", "path", "network_endpoint", "file_upload"]
    required_for_session: bool = False
    # Conditional requiredness: required only when each listed device_config field
    # equals the given value (e.g. {prefer_devicectl: true}). An unset gated field
    # falls back to its schema default. Use instead of required_for_session when a
    # field is only needed in some modes.
    required_for_session_when: dict[str, Any] = Field(default_factory=dict)
    sensitive: bool = False
    default: str | int | bool | None = None
    capability_name: str | None = None


class ConnectionBehavior(BaseModel):
    """Connection and device-type defaults derived from manifest metadata."""

    model_config = ConfigDict(extra="forbid")

    default_device_type: Literal["real_device", "emulator", "simulator"] | None = None
    default_connection_type: Literal["usb", "network", "virtual"] | None = None
    requires_ip_address: bool = False
    requires_connection_target: bool = True
    allow_transport_identity_until_host_resolution: bool = False
    host_resolution_action: str | None = None


class ManifestParallelResourcePort(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_name: str
    start: int
    # Skip allocating this port when each listed device_config field equals the
    # given value. An unset gated field falls back to its schema default (same
    # semantics as required_for_session_when). Fields not declared in the
    # device's effective fields schema never match, so the gate only binds
    # device types that expose the field.
    skip_when: dict[str, Any] = Field(default_factory=dict)


class ParallelResources(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ports: list[ManifestParallelResourcePort] = Field(default_factory=list)
    derived_data_path: bool = False


class LifecycleAction(BaseModel):
    """A lifecycle action exposed by a platform adapter."""

    model_config = ConfigDict(extra="forbid")

    id: Literal["state", "reconnect", "boot", "shutdown", "release_forwarded_ports"]


class HealthCheckAppliesWhen(BaseModel):
    """Gate a health check to specific connection types and IP address presence."""

    model_config = ConfigDict(extra="forbid")

    connection_types: list[Literal["usb", "network", "virtual"]]
    requires_ip_address: bool = False

    @model_validator(mode="after")
    def _non_empty_connection_types(self) -> "HealthCheckAppliesWhen":
        if not self.connection_types:
            raise ValueError("applies_when.connection_types must not be empty")
        return self


class HealthCheckLabel(BaseModel):
    """Display metadata for a health check emitted by a pack adapter."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    applies_when: HealthCheckAppliesWhen | None = None
    # When true, the backend debounces transient failures of this check with a
    # consecutive-failure counter before marking the device unhealthy (mirrors the
    # ip_ping hysteresis). Use for flaky reachability probes — e.g. Roku ECP on port
    # 8060 — that blip while the device is asleep but recover on their own. Hard
    # control-channel checks (adb) should leave this false so failures act immediately.
    debounce: bool = False

    @model_validator(mode="after")
    def _non_empty(self) -> "HealthCheckLabel":
        if not self.id.strip():
            raise ValueError("health check id must not be empty")
        if not self.label.strip():
            raise ValueError("health check label must not be empty")
        return self


class PlatformDisplay(BaseModel):
    """Optional UI display metadata for a platform."""

    model_config = ConfigDict(extra="forbid")

    icon_kind: Literal["mobile", "tv", "set_top", "generic"] | None = None


class PlatformDeviceTypeOverride(BaseModel):
    """Device-type-specific metadata for a platform lane."""

    model_config = ConfigDict(extra="forbid")

    identity: Identity | None = None
    device_fields_schema: list[FieldSchema] | None = None
    lifecycle_actions: list[LifecycleAction] | None = None
    default_capabilities: dict[str, str | int | bool] | None = None
    connection_behavior: ConnectionBehavior | None = None

    @model_validator(mode="after")
    def _check_default_capability_templates(self) -> "PlatformDeviceTypeOverride":
        for key, value in (self.default_capabilities or {}).items():
            if not isinstance(value, str):
                continue
            for match in _TEMPLATE_VAR_RE.finditer(value):
                var = match.group(1)
                if var not in ALLOWED_TEMPLATE_VARS:
                    raise ValueError(
                        f"device_type_overrides: default_capabilities[{key!r}] uses unknown "
                        f"template variable {{{var}}}; allowed: {sorted(ALLOWED_TEMPLATE_VARS)}"
                    )
        return self


class Platform(BaseModel):
    """A target platform (Android real device, iOS simulator, etc.) supported by this pack."""

    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    automation_name: str
    appium_platform_name: str
    device_types: list[str]
    connection_types: list[str]
    capabilities: Capabilities
    identity: Identity
    device_fields_schema: list[FieldSchema] = Field(default_factory=list)
    host_fields_schema: list[FieldSchema] = Field(default_factory=list)
    lifecycle_actions: list[LifecycleAction] = Field(default_factory=list)
    health_checks: list[HealthCheckLabel] = Field(default_factory=list)
    display: PlatformDisplay | None = None
    default_capabilities: dict[str, str | int | bool] = Field(default_factory=dict)
    connection_behavior: ConnectionBehavior = Field(default_factory=ConnectionBehavior)
    parallel_resources: ParallelResources = Field(default_factory=ParallelResources)
    device_type_overrides: dict[
        Literal["real_device", "emulator", "simulator"],
        PlatformDeviceTypeOverride,
    ] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_default_capability_templates(self) -> "Platform":
        for key, value in self.default_capabilities.items():
            if not isinstance(value, str):
                continue
            for match in _TEMPLATE_VAR_RE.finditer(value):
                var = match.group(1)
                if var not in ALLOWED_TEMPLATE_VARS:
                    raise ValueError(
                        f"platform {self.id}: default_capabilities[{key!r}] uses unknown "
                        f"template variable {{{var}}}; allowed: {sorted(ALLOWED_TEMPLATE_VARS)}"
                    )
        return self


class DoctorCheck(BaseModel):
    """A diagnostic check for driver health."""

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    adapter_hook: str | None = None


class ToolDependency(BaseModel):
    """A host tool required by this driver pack."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str


class Requires(BaseModel):
    """Version requirements for runtime dependencies."""

    model_config = ConfigDict(extra="forbid")

    host_os: list[Literal["linux", "macos"]] = Field(default_factory=list)
    tool_dependencies: list[ToolDependency] = Field(default_factory=list)


class AppiumEnvAppliesWhen(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform_ids: list[str] = Field(default_factory=list)
    device_types: list[str] = Field(default_factory=list)
    min_os_version: str | None = None
    # Gate on per-device config: the rule applies only when each listed device
    # field equals the given value. An unset device field is treated as that
    # value, so the value here encodes the default (opt-out) behavior.
    device_config: dict[str, Any] = Field(default_factory=dict)


class AppiumEnvRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    applies_when: AppiumEnvAppliesWhen = Field(default_factory=AppiumEnvAppliesWhen)
    env: dict[str, str] = Field(default_factory=dict)


class FeatureManifest(BaseModel):
    """Descriptor for a single feature declared in a driver-pack manifest.

    Extra keys are permitted so that pack authors can include arbitrary
    feature-specific configuration fields alongside the standard ones.
    """

    model_config = ConfigDict(extra="allow")

    display_name: str
    description_md: str = ""
    help_url: str | None = None
    applies_when: dict[str, Any] = Field(default_factory=dict)
    requirements: dict[str, Any] = Field(default_factory=dict)
    sidecar: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = Field(default_factory=list)


class RuntimePackage(BaseModel):
    """An extra npm package the agent installs into the Appium runtime.

    Used for driver companion libraries (e.g. appium-ios-remotexpc) that npm
    treats as an optional dependency and may silently skip in the agent's
    headless install environment. Declaring them here installs them explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    package: str
    version: str


class Manifest(BaseModel):
    """A driver-pack manifest describing Appium server, driver, platforms, and diagnostics."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    id: str
    release: str
    display_name: str
    maintainer: str = ""
    license: str = ""
    requires: Requires = Field(default_factory=Requires)
    appium_server: AppiumInstallable
    appium_driver: AppiumInstallable
    platforms: list[Platform]
    doctor: list[DoctorCheck] = []
    insecure_features: list[str] = Field(default_factory=list)
    appium_env: list[AppiumEnvRule] = Field(default_factory=list)
    runtime_packages: list[RuntimePackage] = Field(default_factory=list)
    features: dict[str, FeatureManifest] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _gate_keys_reference_known_fields(self) -> "Manifest":
        """Reject device_config gate keys that don't name a declared device field
        — a typo'd key would otherwise silently never match (always-apply for
        appium_env, never-required for required_for_session_when)."""
        all_ids: set[str] = set()
        schemas: list[list[FieldSchema]] = []
        for plat in self.platforms:
            schemas.append(plat.device_fields_schema)
            for override in plat.device_type_overrides.values():
                if override.device_fields_schema:
                    schemas.append(override.device_fields_schema)
        for schema in schemas:
            all_ids.update(field.id for field in schema)
        for rule in self.appium_env:
            for key in rule.applies_when.device_config:
                if key not in all_ids:
                    raise ValueError(f"appium_env rule '{rule.id}' gates on unknown device field '{key}'")
        for schema in schemas:
            for field_def in schema:
                for key in field_def.required_for_session_when:
                    if key not in all_ids:
                        raise ValueError(
                            f"field '{field_def.id}' required_for_session_when gates on unknown device field '{key}'"
                        )
        for plat in self.platforms:
            for port in plat.parallel_resources.ports:
                for key in port.skip_when:
                    if key not in all_ids:
                        raise ValueError(
                            f"port '{port.capability_name}' skip_when gates on unknown device field '{key}'"
                        )
        return self


def load_manifest_yaml(text: str) -> Manifest:
    """Load and validate a driver-pack manifest from YAML text.

    Args:
        text: YAML text to parse

    Returns:
        Validated Manifest object

    Raises:
        ManifestValidationError: If YAML is invalid or schema validation fails
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestValidationError(f"Failed to parse manifest YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestValidationError("Manifest YAML must be a dictionary at the top level")

    try:
        return Manifest.model_validate(raw)
    except ValidationError as exc:
        raise ManifestValidationError(str(exc)) from exc
