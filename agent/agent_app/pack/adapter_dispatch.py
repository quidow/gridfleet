"""Async dispatch wrappers around DriverPackAdapter hooks.

Each wrapper:
- Enforces a hard timeout via ``asyncio.wait_for``.
- Translates ``TimeoutError`` into ``AdapterHookTimeoutError``.
- Wraps any other exception from the adapter as ``AdapterHookExecutionError``.
- Validates the return type; raises ``AdapterContractError`` on mismatch.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

from agent_app.pack.adapter_types import (
    DiscoveryCandidate,
    DiscoveryContext,
    DoctorCheckResult,
    DoctorContext,
    DriverPackAdapter,
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

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

ADAPTER_HOOK_TIMEOUT_SECONDS: float = 30.0


class AdapterHookTimeoutError(Exception):
    """Raised when an adapter hook does not return within the deadline."""

    def __init__(self, hook: str, pack_id: str, pack_release: str) -> None:
        super().__init__(
            f"adapter hook {hook!r} timed out after {ADAPTER_HOOK_TIMEOUT_SECONDS}s "
            f"(pack={pack_id!r} release={pack_release!r})"
        )
        self.hook = hook
        self.pack_id = pack_id
        self.pack_release = pack_release


class AdapterHookExecutionError(Exception):
    """Raised when an adapter hook raises an unexpected exception."""

    def __init__(self, hook: str, pack_id: str, pack_release: str, cause: Exception) -> None:
        super().__init__(
            f"adapter hook {hook!r} raised {type(cause).__name__}: {cause} (pack={pack_id!r} release={pack_release!r})"
        )
        self.hook = hook
        self.pack_id = pack_id
        self.pack_release = pack_release
        self.__cause__ = cause


class AdapterContractError(Exception):
    """Raised when an adapter hook returns a value that violates the protocol contract."""

    def __init__(self, hook: str, pack_id: str, pack_release: str, detail: str) -> None:
        super().__init__(
            f"adapter hook {hook!r} violated contract: {detail} (pack={pack_id!r} release={pack_release!r})"
        )
        self.hook = hook
        self.pack_id = pack_id
        self.pack_release = pack_release


async def _call_hook[T](
    adapter: DriverPackAdapter,
    hook: str,
    call: Callable[[], Awaitable[object]],
    expected: type[T],
) -> T:
    """Run one adapter hook with timeout, exception wrapping, and contract check."""
    try:
        result = await asyncio.wait_for(call(), timeout=ADAPTER_HOOK_TIMEOUT_SECONDS)
    except TimeoutError:
        raise AdapterHookTimeoutError(hook, adapter.pack_id, adapter.pack_release) from None
    except AdapterHookTimeoutError:
        raise
    except Exception as exc:
        raise AdapterHookExecutionError(hook, adapter.pack_id, adapter.pack_release, exc) from exc
    if not isinstance(result, expected):
        raise AdapterContractError(
            hook,
            adapter.pack_id,
            adapter.pack_release,
            f"expected {expected.__name__}, got {type(result).__name__}",
        )
    return result


async def dispatch_discover(
    adapter: DriverPackAdapter,
    ctx: DiscoveryContext,
) -> list[DiscoveryCandidate]:
    """Call ``adapter.discover`` with timeout + contract enforcement."""
    return await _call_hook(adapter, "discover", lambda: adapter.discover(ctx), list)


async def dispatch_doctor(
    adapter: DriverPackAdapter,
    ctx: DoctorContext,
) -> list[DoctorCheckResult]:
    """Call ``adapter.doctor`` with timeout + contract enforcement."""
    return await _call_hook(adapter, "doctor", lambda: adapter.doctor(ctx), list)


async def dispatch_health_check(
    adapter: DriverPackAdapter,
    ctx: HealthContext,
) -> list[HealthCheckResult]:
    """Call ``adapter.health_check`` with timeout + contract enforcement."""
    return await _call_hook(adapter, "health_check", lambda: adapter.health_check(ctx), list)


async def dispatch_lifecycle_action(
    adapter: DriverPackAdapter,
    action_id: str,
    args: dict[str, Any],
    ctx: LifecycleContext,
) -> LifecycleActionResult:
    """Call ``adapter.lifecycle_action`` with timeout + contract enforcement."""
    return await _call_hook(
        adapter,
        "lifecycle_action",
        lambda: adapter.lifecycle_action(action_id, args, ctx),  # type: ignore[arg-type]
        LifecycleActionResult,
    )


async def dispatch_pre_session(
    adapter: DriverPackAdapter,
    spec: SessionSpec,
) -> dict[str, Any]:
    """Call ``adapter.pre_session`` with timeout + contract enforcement."""
    return await _call_hook(adapter, "pre_session", lambda: adapter.pre_session(spec), dict)


async def dispatch_post_session(
    adapter: DriverPackAdapter,
    spec: SessionSpec,
    outcome: SessionOutcome,
) -> None:
    """Call ``adapter.post_session`` with timeout + contract enforcement."""
    await _call_hook(adapter, "post_session", lambda: adapter.post_session(spec, outcome), object)


async def dispatch_feature_action(
    adapter: DriverPackAdapter,
    feature_id: str,
    action_id: str,
    args: dict[str, Any],
    ctx: LifecycleContext,
) -> FeatureActionResult:
    """Call ``adapter.feature_action`` with timeout + contract enforcement."""
    return await _call_hook(
        adapter,
        "feature_action",
        lambda: adapter.feature_action(feature_id, action_id, args, ctx),
        FeatureActionResult,
    )


async def dispatch_sidecar_lifecycle(
    adapter: DriverPackAdapter,
    feature_id: str,
    action: Literal["start", "stop", "status"],
) -> SidecarStatus:
    """Call ``adapter.sidecar_lifecycle`` with timeout + contract enforcement."""
    return await _call_hook(
        adapter,
        "sidecar_lifecycle",
        lambda: adapter.sidecar_lifecycle(feature_id, action),
        SidecarStatus,
    )


async def dispatch_normalize_device(
    adapter: DriverPackAdapter,
    ctx: NormalizeDeviceContext,
) -> NormalizedDevice:
    """Call ``adapter.normalize_device`` with timeout + contract enforcement."""
    return await _call_hook(adapter, "normalize_device", lambda: adapter.normalize_device(ctx), NormalizedDevice)


async def dispatch_telemetry(
    adapter: DriverPackAdapter,
    ctx: TelemetryContext,
) -> HardwareTelemetry:
    """Call ``adapter.telemetry`` with timeout + contract enforcement."""
    return await _call_hook(adapter, "telemetry", lambda: adapter.telemetry(ctx), HardwareTelemetry)
