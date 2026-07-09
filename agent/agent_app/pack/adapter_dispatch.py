"""Async dispatch wrappers around DriverPackAdapter hooks.

Each wrapper:
- Enforces a hard timeout via ``asyncio.wait_for``.
- Translates ``TimeoutError`` into ``AdapterHookTimeoutError``.
- Wraps any other exception from the adapter as ``AdapterHookExecutionError``.
- Validates the return type; raises ``AdapterContractError`` on mismatch.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from agent_app.pack.adapter_types import (
    DiscoveryCandidate,
    DiscoveryContext,
    DoctorCheckResult,
    DoctorContext,
    DriverPackAdapter,
    HardwareTelemetry,
    HealthCheckResult,
    HealthContext,
    LifecycleActionResult,
    LifecycleContext,
    NormalizedDevice,
    NormalizeDeviceContext,
    SessionOutcome,
    SessionSpec,
    TelemetryContext,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agent_app.pack.manifest import DesiredPack

ADAPTER_HOOK_TIMEOUT_SECONDS: float = 30.0


def adapter_supports(adapter: object, hook: str) -> bool:
    """True when *adapter* implements the named hook.

    Curated adapters are plain classes that implement only the hooks they
    support — they do NOT subclass the ``DriverPackAdapter`` Protocol — so a
    ``hasattr`` probe is a truthful capability check. A missing optional hook
    therefore routes through the same "no adapter" branch as a pack that ships
    no adapter at all, instead of raising an ``AdapterHookExecutionError``.
    """
    return hasattr(adapter, hook)


# (manifest declaration that implies a hook, required adapter hook). health_check
# is intentionally absent: the agent manifest carries no health-check declaration,
# so it cannot be cross-checked at load time.
_DECLARATION_HOOKS: tuple[tuple[str, str], ...] = (("lifecycle_actions", "lifecycle_action"),)


def missing_declared_hooks(pack: DesiredPack, adapter: object) -> list[str]:
    """Adapter hooks the manifest declares capabilities for but the adapter lacks.

    A non-empty result is a pack-authoring error (the manifest promises behavior
    the adapter cannot deliver) and blocks the pack at load, mirroring how a
    runtime-resolution failure blocks it.
    """
    declares = {
        "lifecycle_actions": any(platform.lifecycle_actions for platform in pack.platforms),
    }
    return [
        hook
        for declaration, hook in _DECLARATION_HOOKS
        if declares[declaration] and not adapter_supports(adapter, hook)
    ]


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
