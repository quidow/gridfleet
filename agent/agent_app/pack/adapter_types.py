"""Adapter contract types for DriverPackAdapter hooks.

Mirrors the type definitions in ``backend/app/pack/adapter.py`` so the agent
can validate adapter return values without depending on the backend package.
These dataclasses define the wire contract that every DriverPackAdapter must
honour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass
class FieldError:
    field_id: str
    message: str


@dataclass
class FeatureStatus:
    feature_id: str
    ok: bool
    detail: str = ""


@dataclass
class DiscoveryCandidate:
    identity_scheme: str
    identity_value: str
    suggested_name: str
    detected_properties: dict[str, Any]
    runnable: bool
    missing_requirements: list[str]
    field_errors: list[FieldError]
    feature_status: list[FeatureStatus]


@dataclass
class HealthCheckResult:
    check_id: str
    ok: bool
    detail: str = ""


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


@dataclass
class FeatureActionResult:
    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class SidecarStatus:
    ok: bool
    detail: str = ""
    state: str = ""


class DiscoveryContext(Protocol):
    host_id: str
    platform_id: str


class HealthContext(Protocol):
    device_identity_value: str
    allow_boot: bool
    platform_id: str | None
    device_type: str | None
    connection_type: str | None


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
    pack_id: str
    pack_release: str

    async def discover(self, ctx: DiscoveryContext) -> list[DiscoveryCandidate]:
        pass

    async def doctor(self, ctx: DoctorContext) -> list[DoctorCheckResult]:
        pass

    async def health_check(self, ctx: HealthContext) -> list[HealthCheckResult]:
        pass

    async def lifecycle_action(
        self,
        action_id: Literal["reconnect", "boot", "shutdown", "state"],
        args: dict[str, Any],
        ctx: LifecycleContext,
    ) -> LifecycleActionResult:
        pass

    async def pre_session(self, spec: SessionSpec) -> dict[str, Any]:
        pass

    async def post_session(self, spec: SessionSpec, outcome: SessionOutcome) -> None:
        pass

    async def feature_action(
        self,
        feature_id: str,
        action_id: str,
        args: dict[str, Any],
        ctx: LifecycleContext,
    ) -> FeatureActionResult:
        pass

    async def sidecar_lifecycle(
        self,
        feature_id: str,
        action: Literal["start", "stop", "status"],
    ) -> SidecarStatus:
        pass

    async def normalize_device(self, ctx: NormalizeDeviceContext) -> NormalizedDevice:
        pass

    async def telemetry(self, ctx: TelemetryContext) -> HardwareTelemetry:
        pass
