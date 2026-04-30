"""Roku (ECP) driver pack adapter."""

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
from agent_app.pack.adapter_utils import tcp_reachable


class Adapter:
    pack_id: str = ""
    pack_release: str = ""
    discovery_scope: str = "pack"

    async def discover(self, ctx: DiscoveryContext) -> list[DiscoveryCandidate]:
        from adapter.discovery import discover_roku_devices

        return await discover_roku_devices(ctx)

    async def doctor(self, ctx: DoctorContext) -> list[DoctorCheckResult]:
        return []

    async def health_check(self, ctx: HealthContext) -> list[HealthCheckResult]:
        target = ctx.device_identity_value
        reachable = await tcp_reachable(target, 8060, timeout=5.0)
        return [
            HealthCheckResult(
                check_id="ping",
                ok=reachable,
                detail="" if reachable else "Roku ECP port 8060 unreachable",
            ),
            HealthCheckResult(
                check_id="ecp",
                ok=reachable,
                detail="" if reachable else "Roku ECP port 8060 unreachable",
            ),
        ]

    async def lifecycle_action(
        self,
        action_id: Literal["reconnect", "boot", "shutdown", "state"],
        args: dict[str, Any],
        ctx: LifecycleContext,
    ) -> LifecycleActionResult:
        if action_id == "state":
            reachable = await tcp_reachable(ctx.device_identity_value, 8060, timeout=5.0)
            return LifecycleActionResult(ok=True, state="reachable" if reachable else "unreachable")
        return LifecycleActionResult(ok=False, detail=f"Unsupported: {action_id}")

    async def pre_session(self, spec: SessionSpec) -> dict[str, Any]:
        return {}

    async def post_session(self, spec: SessionSpec, outcome: SessionOutcome) -> None:
        return None

    async def normalize_device(self, ctx: NormalizeDeviceContext) -> NormalizedDevice:
        from adapter.normalize import normalize_device

        return await normalize_device(ctx)

    async def telemetry(self, ctx: TelemetryContext) -> HardwareTelemetry:
        return HardwareTelemetry(supported=False)

    async def feature_action(
        self,
        feature_id: str,
        action_id: str,
        args: dict[str, Any],
        ctx: LifecycleContext,
    ) -> FeatureActionResult:
        return FeatureActionResult(ok=False, detail="No feature actions")

    async def sidecar_lifecycle(
        self,
        feature_id: str,
        action: Literal["start", "stop", "status"],
    ) -> SidecarStatus:
        return SidecarStatus(ok=False, detail="No sidecars")
