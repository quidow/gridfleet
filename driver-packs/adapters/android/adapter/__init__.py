"""Android (ADB) driver pack adapter."""

from __future__ import annotations

import shutil
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
        from adapter.discovery import discover_adb_devices

        return await discover_adb_devices(ctx)

    async def doctor(self, ctx: DoctorContext) -> list[DoctorCheckResult]:
        from adapter.tools import find_adb, find_android_home

        adb = find_adb()
        adb_ok = shutil.which(adb) is not None or adb != "adb"
        home = find_android_home()
        return [
            DoctorCheckResult(check_id="adb", ok=adb_ok, message="" if adb_ok else "adb not found"),
            DoctorCheckResult(
                check_id="android_home", ok=home is not None, message="" if home else "ANDROID_HOME not set"
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
        if feature_id == "adb_monitor":
            from adapter.sidecar import sidecar_lifecycle

            return await sidecar_lifecycle(action)
        return SidecarStatus(ok=False, detail=f"Unknown feature: {feature_id}")
