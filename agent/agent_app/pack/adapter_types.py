"""Adapter contract types for DriverPackAdapter hooks.

These dataclasses define the wire contract that every DriverPackAdapter must
honour. The JSON-lines encoding lives in :mod:`agent_app.pack.worker_protocol`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass
class FieldError:
    field_id: str
    message: str


@dataclass
class DiscoveryCandidate:
    identity_scheme: str
    identity_value: str
    suggested_name: str
    detected_properties: dict[str, Any]
    runnable: bool
    missing_requirements: list[str]
    field_errors: list[FieldError]


@dataclass
class HealthCheckResult:
    check_id: str
    ok: bool
    detail: str = ""
    # Optional name of a manifest-declared lifecycle action the adapter
    # recommends as remediation (generic; core dispatches without interpreting).
    recommended_action: str | None = None
    # Transient failures of this check are hysteresis-debounced by the backend.
    debounce: bool = False


@dataclass
class DoctorCheckResult:
    check_id: str
    ok: bool
    message: str = ""


@dataclass
class LifecycleActionResult:
    ok: bool
    state: str = ""
    detail: str = ""
    resolved_connection_target: str | None = None


@dataclass
class SubprocessEnvContribution:
    env_vars: dict[str, str] = field(default_factory=dict)
    extra_path_dirs: list[str] = field(default_factory=list)


class DiscoveryContext(Protocol):
    host_id: str
    platform_id: str


class HealthContext(Protocol):
    device_identity_value: str
    allow_boot: bool
    # Expected device identity (serial) for adapters that can verify it at the
    # probed target; ``None`` when the caller has no confirmed identity.
    # Adapters must read it via ``getattr(ctx, "expected_identity_value", None)``
    # so old/new agent-adapter combinations degrade to no verification.
    expected_identity_value: str | None
    platform_id: str | None
    device_type: str | None
    connection_type: str | None
    ip_address: str | None
    ip_ping_timeout_sec: float | None
    ip_ping_count: int | None
    # Manifest-claimed parallel-resource ports for the device's node, keyed by
    # capability name (e.g. "appium:systemPort"); None when the caller supplies
    # no claims. ``has_live_session`` is False only when the control plane
    # positively knows no client session or viability probe is live for this
    # device; None = unknown. Adapters must read both via
    # ``getattr(ctx, "...", None)`` so old/new agent-adapter combos degrade to
    # skipping port checks.
    claimed_ports: dict[str, int] | None
    has_live_session: bool | None


class DoctorContext(Protocol):
    host_id: str


class LifecycleContext(Protocol):
    host_id: str
    device_identity_value: str


@dataclass
class SessionSpec:
    pack_id: str
    platform_id: str
    device_identity_value: str
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionOutcome:
    ok: bool
    detail: str = ""


class NormalizeDeviceContext(Protocol):
    host_id: str
    platform_id: str
    raw_input: dict[str, Any]


@dataclass
class NormalizedDevice:
    identity_scheme: str
    identity_scope: str
    identity_value: str
    connection_target: str
    ip_address: str
    device_type: str
    connection_type: str
    os_version: str
    field_errors: list[FieldError]
    os_version_display: str | None = None
    manufacturer: str = ""
    model: str = ""
    model_number: str = ""
    software_versions: dict[str, str] = field(default_factory=dict)


class TelemetryContext(Protocol):
    device_identity_value: str
    connection_target: str


@dataclass
class HardwareTelemetry:
    supported: bool
    battery_level_percent: int | None = None
    battery_temperature_c: float | None = None
    charging_state: str | None = None


class DriverPackAdapter(Protocol):
    """The full surface a driver-pack adapter *may* implement.

    A curated adapter is a plain ``Adapter`` class — it does NOT subclass this
    Protocol; the Protocol only documents the contract and gives the core type
    checks something to validate against. Because adapters are plain classes,
    the worker handshake advertises the hooks each adapter implements, so an
    adapter ships only the hooks it needs.

    Required core (every pack must implement both):
      - ``discover``          — enumerate candidate devices on the host.
      - ``normalize_device``  — turn raw operator input into the canonical shape.

    Everything below is optional. A missing optional hook is treated exactly
    like a pack that ships no adapter for that concern (the dispatch site takes
    its no-adapter branch); it never surfaces as a hook-execution error. If the
    manifest *declares* a capability whose hook is absent, the load-time
    cross-check (``missing_declared_hooks``) blocks the pack instead.

    Optional hook groups:
      - health:            ``health_check``, ``doctor``
      - lifecycle:         ``lifecycle_action``
      - sessions:          ``pre_session``, ``post_session``
      - telemetry:         ``telemetry``
      - environment:       ``subprocess_env``, ``tool_versions``
    """

    pack_id: str
    pack_release: str

    # --- Required core -----------------------------------------------------
    async def discover(self, ctx: DiscoveryContext) -> list[DiscoveryCandidate]:
        raise NotImplementedError

    async def normalize_device(self, ctx: NormalizeDeviceContext) -> NormalizedDevice:
        raise NotImplementedError

    # --- Optional: health --------------------------------------------------
    async def health_check(self, ctx: HealthContext) -> list[HealthCheckResult]:
        raise NotImplementedError

    async def doctor(self, ctx: DoctorContext) -> list[DoctorCheckResult]:
        raise NotImplementedError

    # --- Optional: lifecycle ----------------------------------------------
    async def lifecycle_action(
        self,
        action_id: Literal["reconnect", "boot", "shutdown", "state", "release_forwarded_ports"],
        args: dict[str, Any],
        ctx: LifecycleContext,
    ) -> LifecycleActionResult:
        raise NotImplementedError

    # --- Optional: sessions ------------------------------------------------
    async def pre_session(self, spec: SessionSpec) -> dict[str, Any]:
        raise NotImplementedError

    async def post_session(self, spec: SessionSpec, outcome: SessionOutcome) -> None:
        raise NotImplementedError

    # --- Optional: telemetry ----------------------------------------------
    async def telemetry(self, ctx: TelemetryContext) -> HardwareTelemetry:
        raise NotImplementedError

    # --- Optional: environment --------------------------------------------
    def subprocess_env(self) -> SubprocessEnvContribution:
        return SubprocessEnvContribution()

    def tool_versions(self) -> dict[str, str | None]:
        return {}
