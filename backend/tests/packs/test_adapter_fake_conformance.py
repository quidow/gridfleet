from typing import Any

import pytest

from app.packs.adapter import (
    DiscoveryCandidate,
    DiscoveryContext,
    DoctorCheckResult,
    DoctorContext,
    DriverPackAdapter,
    FeatureActionResult,
    HealthCheckResult,
    HealthContext,
    LifecycleActionResult,
    LifecycleContext,
    SessionOutcome,
    SessionSpec,
    SidecarStatus,
)


class FakeAdapter:
    pack_id = "fake"
    pack_release = "0.0.1"

    async def discover(self, ctx: DiscoveryContext) -> list[DiscoveryCandidate]:
        return []

    async def doctor(self, ctx: DoctorContext) -> list[DoctorCheckResult]:
        return [DoctorCheckResult(check_id="fake", ok=True)]

    async def health_check(self, ctx: HealthContext) -> list[HealthCheckResult]:
        return [HealthCheckResult(check_id="fake", ok=True)]

    async def feature_action(
        self,
        feature_id: str,
        action_id: str,
        args: dict[str, Any],
        ctx: LifecycleContext,
    ) -> FeatureActionResult:
        return FeatureActionResult(ok=True)

    async def lifecycle_action(
        self,
        action_id: str,
        args: dict[str, Any],
        ctx: LifecycleContext,
    ) -> LifecycleActionResult:
        return LifecycleActionResult(ok=True, state="online")

    async def pre_session(self, spec: SessionSpec) -> dict[str, Any]:
        return {"fake_cap": True}

    async def post_session(self, spec: SessionSpec, outcome: SessionOutcome) -> None:
        return None

    async def sidecar_lifecycle(self, feature_id: str, action: str) -> SidecarStatus:
        return SidecarStatus(ok=True, state="running")


def _as_protocol(a: DriverPackAdapter) -> DriverPackAdapter:
    return a


@pytest.mark.asyncio
async def test_fake_adapter_satisfies_protocol_and_exercises_each_method() -> None:
    adapter = _as_protocol(FakeAdapter())

    class _DiscoveryCtx:
        host_id = "h"
        platform_id = "p"

    class _DoctorCtx:
        host_id = "h"

    class _HealthCtx:
        device_identity_value = "d"
        allow_boot = False

    class _LifecycleCtx:
        host_id = "h"
        device_identity_value = "d"

    assert await adapter.discover(_DiscoveryCtx()) == []
    doctor = await adapter.doctor(_DoctorCtx())
    assert doctor[0].ok
    health = await adapter.health_check(_HealthCtx())
    assert health[0].ok
    feat = await adapter.feature_action("f", "a", {}, _LifecycleCtx())
    assert feat.ok
    life = await adapter.lifecycle_action("state", {}, _LifecycleCtx())
    assert life.ok
    spec = SessionSpec(pack_id="fake", platform_id="p", device_identity_value="d")
    caps = await adapter.pre_session(spec)
    assert caps == {"fake_cap": True}
    await adapter.post_session(spec, SessionOutcome(ok=True))
    sidecar = await adapter.sidecar_lifecycle("feature", "start")
    assert sidecar.ok
