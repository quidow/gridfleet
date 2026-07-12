"""Roku (ECP) driver pack adapter."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from agent_app.pack.adapter_types import (
    DiscoveryCandidate,
    DiscoveryContext,
    HealthCheckResult,
    HealthContext,
    LifecycleActionResult,
    LifecycleContext,
    NormalizedDevice,
    NormalizeDeviceContext,
)
from agent_app.pack.adapter_utils import tcp_reachable

_HEALTH_PROBE_RETRY_BACKOFF_SEC = 0.5


async def _tcp_reachable_with_retry(host: str, port: int, *, timeout: float = 5.0) -> bool:
    if await tcp_reachable(host, port, timeout=timeout):
        return True
    await asyncio.sleep(_HEALTH_PROBE_RETRY_BACKOFF_SEC)
    return await tcp_reachable(host, port, timeout=timeout)


async def _verify_identity(target: str, expected: str) -> HealthCheckResult | None:
    """Compare the serial reported at ``target`` with the expected identity.

    Only a definitive mismatch fails (a different device answering on a
    reused address); an inconclusive ECP query — timeout, missing serial —
    reports nothing, so transient device-info failures never flap health.
    """
    from .normalize import fetch_device_info

    try:
        info = await fetch_device_info(target)
    except Exception:  # noqa: BLE001 — inconclusive, not a health failure
        return None
    serial = info.get("serial-number") or info.get("device-id") or ""
    if not serial:
        return None
    ok = serial == expected
    return HealthCheckResult(
        check_id="identity",
        ok=ok,
        detail="" if ok else f"Device at target reports serial {serial}, expected {expected}",
    )


class Adapter:
    pack_id: str = ""
    pack_release: str = ""
    discovery_scope: str = "pack"

    async def discover(self, ctx: DiscoveryContext) -> list[DiscoveryCandidate]:
        from .discovery import discover_roku_devices

        return await discover_roku_devices(ctx)

    async def health_check(self, ctx: HealthContext) -> list[HealthCheckResult]:
        target = ctx.device_identity_value
        # Single-shot TCP probes were flipping ``ping``/``ecp`` to unhealthy
        # on transient network blips during high-traffic sessions. One retry
        # with a short backoff debounces these without hiding genuine outages.
        reachable = await _tcp_reachable_with_retry(target, 8060, timeout=5.0)
        results = [
            HealthCheckResult(
                check_id="ping",
                ok=reachable,
                detail="" if reachable else "Roku ECP port 8060 unreachable",
                debounce=True,
            ),
            HealthCheckResult(
                check_id="ecp",
                ok=reachable,
                detail="" if reachable else "Roku ECP port 8060 unreachable",
                debounce=True,
            ),
        ]
        expected = getattr(ctx, "expected_identity_value", None)
        if reachable and expected:
            identity = await _verify_identity(target, str(expected))
            if identity is not None:
                results.append(identity)
        return results

    async def lifecycle_action(
        self,
        action_id: Literal["reconnect", "boot", "shutdown", "state", "release_forwarded_ports"],
        args: dict[str, Any],
        ctx: LifecycleContext,
    ) -> LifecycleActionResult:
        if action_id == "state":
            reachable = await tcp_reachable(ctx.device_identity_value, 8060, timeout=5.0)
            return LifecycleActionResult(ok=True, state="reachable" if reachable else "unreachable")
        return LifecycleActionResult(ok=False, detail=f"Unsupported: {action_id}")

    async def normalize_device(self, ctx: NormalizeDeviceContext) -> NormalizedDevice:
        from .normalize import normalize_device

        return await normalize_device(ctx)
