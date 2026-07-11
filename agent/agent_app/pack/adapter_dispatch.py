"""Dispatch adapter hooks through supervised workers.

The small legacy fallback is retained for isolated unit-test doubles that
implement the old plain-adapter shape; production registries contain only
``WorkerHandle`` instances.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import TYPE_CHECKING, Any, cast

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
    from agent_app.pack.worker_supervisor import WorkerHandle

ADAPTER_HOOK_TIMEOUT_SECONDS: float = 30.0


def adapter_supports(handle: object, hook: str) -> bool:
    """True when a worker handshake (or a legacy test double) advertises *hook*."""
    supported = getattr(handle, "supported_hooks", None)
    if supported is not None:
        return hook in supported
    return hasattr(handle, hook)


_DECLARATION_HOOKS: tuple[tuple[str, str], ...] = (("lifecycle_actions", "lifecycle_action"),)


def missing_declared_hooks(pack: DesiredPack, handle: object) -> list[str]:
    declares = {"lifecycle_actions": any(platform.lifecycle_actions for platform in pack.platforms)}
    return [
        hook for declaration, hook in _DECLARATION_HOOKS if declares[declaration] and not adapter_supports(handle, hook)
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
    """Raised when an adapter hook returns a value that violates the contract."""

    def __init__(self, hook: str, pack_id: str, pack_release: str, detail: str) -> None:
        super().__init__(
            f"adapter hook {hook!r} violated contract: {detail} (pack={pack_id!r} release={pack_release!r})"
        )
        self.hook = hook
        self.pack_id = pack_id
        self.pack_release = pack_release


def _context_payload(ctx: object) -> dict[str, Any]:
    if dataclasses.is_dataclass(ctx):
        return dataclasses.asdict(ctx)  # type: ignore[arg-type]
    return dict(vars(ctx))


def _identity(handle: object) -> tuple[str, str]:
    return str(getattr(handle, "pack_id", "")), str(getattr(handle, "release", getattr(handle, "pack_release", "")))


def _legacy_call(handle: object, method: str, *args: object) -> Awaitable[object]:
    fn = cast("Callable[..., Awaitable[object]]", getattr(handle, method))
    return fn(*args)


async def _call_hook[T](
    handle: object,
    hook: str,
    payload: dict[str, Any],
    expected: type[T],
    legacy_call: Callable[[], Awaitable[object]],
) -> T:
    pack_id, release = _identity(handle)
    try:
        if hasattr(handle, "call"):
            call = cast("Callable[[str, dict[str, Any]], Awaitable[object]]", handle.call)
            result = await call(hook, payload)
        else:
            result = await asyncio.wait_for(legacy_call(), timeout=ADAPTER_HOOK_TIMEOUT_SECONDS)
    except TimeoutError:
        raise AdapterHookTimeoutError(hook, pack_id, release) from None
    except AdapterHookTimeoutError, AdapterHookExecutionError, AdapterContractError:
        raise
    except Exception as exc:
        raise AdapterHookExecutionError(hook, pack_id, release, exc) from exc
    if not isinstance(result, expected):
        raise AdapterContractError(hook, pack_id, release, f"expected {expected.__name__}, got {type(result).__name__}")
    return result


async def dispatch_discover(
    handle: WorkerHandle | DriverPackAdapter, ctx: DiscoveryContext
) -> list[DiscoveryCandidate]:
    return await _call_hook(
        handle, "discover", {"ctx": _context_payload(ctx)}, list, lambda: _legacy_call(handle, "discover", ctx)
    )


async def dispatch_doctor(handle: WorkerHandle | DriverPackAdapter, ctx: DoctorContext) -> list[DoctorCheckResult]:
    return await _call_hook(
        handle, "doctor", {"ctx": _context_payload(ctx)}, list, lambda: _legacy_call(handle, "doctor", ctx)
    )


async def dispatch_health_check(
    handle: WorkerHandle | DriverPackAdapter, ctx: HealthContext
) -> list[HealthCheckResult]:
    return await _call_hook(
        handle, "health_check", {"ctx": _context_payload(ctx)}, list, lambda: _legacy_call(handle, "health_check", ctx)
    )


async def dispatch_lifecycle_action(
    handle: WorkerHandle | DriverPackAdapter,
    action_id: str,
    args: dict[str, Any],
    ctx: LifecycleContext,
) -> LifecycleActionResult:
    return await _call_hook(
        handle,
        "lifecycle_action",
        {"action_id": action_id, "args": args, "ctx": _context_payload(ctx)},
        LifecycleActionResult,
        lambda: _legacy_call(handle, "lifecycle_action", action_id, args, ctx),
    )


async def dispatch_pre_session(handle: WorkerHandle | DriverPackAdapter, spec: SessionSpec) -> dict[str, Any]:
    return await _call_hook(
        handle,
        "pre_session",
        {"spec": dataclasses.asdict(spec)},
        dict,
        lambda: _legacy_call(handle, "pre_session", spec),
    )


async def dispatch_post_session(
    handle: WorkerHandle | DriverPackAdapter,
    spec: SessionSpec,
    outcome: SessionOutcome,
) -> None:
    await _call_hook(
        handle,
        "post_session",
        {"spec": dataclasses.asdict(spec), "outcome": dataclasses.asdict(outcome)},
        type(None),
        lambda: _legacy_call(handle, "post_session", spec, outcome),
    )


async def dispatch_normalize_device(
    handle: WorkerHandle | DriverPackAdapter,
    ctx: NormalizeDeviceContext,
) -> NormalizedDevice:
    return await _call_hook(
        handle,
        "normalize_device",
        {"ctx": _context_payload(ctx)},
        NormalizedDevice,
        lambda: _legacy_call(handle, "normalize_device", ctx),
    )


async def dispatch_telemetry(handle: WorkerHandle | DriverPackAdapter, ctx: TelemetryContext) -> HardwareTelemetry:
    return await _call_hook(
        handle,
        "telemetry",
        {"ctx": _context_payload(ctx)},
        HardwareTelemetry,
        lambda: _legacy_call(handle, "telemetry", ctx),
    )
