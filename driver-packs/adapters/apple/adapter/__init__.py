"""Apple driver pack adapter."""

from __future__ import annotations

from typing import Any, Literal

from agent_app.pack.adapter_types import (
    DiscoveryCandidate,
    DiscoveryContext,
    DoctorCheckResult,
    DoctorContext,
    FeatureActionResult,
    HardwareTelemetry,
    HealthCheckResult,
    HealthContext,
    LifecycleActionResult,
    LifecycleContext,
    NormalizedDevice,
    NormalizeDeviceContext,
    SessionOutcome,
    SessionSpec,
    SidecarStatus,
    TelemetryContext,
)


class Adapter:
    pack_id: str = ""
    pack_release: str = ""
    discovery_scope: str = "pack"

    async def discover(self, ctx: DiscoveryContext) -> list[DiscoveryCandidate]:
        from adapter.discovery import discover_apple_devices

        return await discover_apple_devices(ctx)

    async def doctor(self, ctx: DoctorContext) -> list[DoctorCheckResult]:
        from adapter.tools import find_go_ios, host_supports_apple_devicectl

        go_ios = find_go_ios()
        return [
            DoctorCheckResult(check_id="xcrun", ok=host_supports_apple_devicectl(), message=""),
            DoctorCheckResult(
                check_id="go_ios",
                ok=bool(go_ios),
                message="go-ios CLI found" if go_ios else "go-ios CLI not found; iOS telemetry unavailable",
            ),
        ]

    async def health_check(self, ctx: HealthContext) -> list[HealthCheckResult]:
        from adapter.health import health_check

        return await health_check(ctx)

    async def lifecycle_action(
        self,
        action_id: Literal["reconnect", "boot", "shutdown", "state"],
        args: dict[str, Any],
        ctx: LifecycleContext,
    ) -> LifecycleActionResult:
        from adapter.lifecycle import lifecycle_action

        return await lifecycle_action(action_id, args, ctx)

    async def pre_session(self, spec: SessionSpec) -> dict[str, Any]:
        from adapter.session import pre_session

        return await pre_session(spec)

    async def post_session(self, spec: SessionSpec, outcome: SessionOutcome) -> None:
        from adapter.session import post_session

        await post_session(spec, outcome)

    async def normalize_device(self, ctx: NormalizeDeviceContext) -> NormalizedDevice:
        from adapter.normalize import normalize_device

        return await normalize_device(ctx)

    async def telemetry(self, ctx: TelemetryContext) -> HardwareTelemetry:
        from adapter.telemetry import collect_telemetry

        return await collect_telemetry(ctx)

    async def feature_action(
        self,
        feature_id: str,
        action_id: str,
        args: dict[str, Any],
        ctx: LifecycleContext,
    ) -> FeatureActionResult:
        return FeatureActionResult(ok=False, detail="No feature actions supported")

    async def sidecar_lifecycle(
        self,
        feature_id: str,
        action: Literal["start", "stop", "status"],
    ) -> SidecarStatus:
        return SidecarStatus(ok=False, detail="No sidecars")
