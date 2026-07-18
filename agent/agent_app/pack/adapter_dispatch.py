"""Dispatch adapter hooks through supervised workers."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from agent_app.pack.adapter_types import (
    DiscoveryCandidate,
    DiscoveryContext,
    DoctorCheckResult,
    DoctorContext,
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
    from agent_app.pack.manifest import DesiredPack, DesiredPlatform
    from agent_app.pack.worker_supervisor import WorkerHandle

ADAPTER_HOOK_TIMEOUT_SECONDS: float = 30.0


def adapter_supports(handle: WorkerHandle, hook: str) -> bool:
    """True when the worker handshake advertises *hook*."""
    return hook in handle.supported_hooks


def _platform_declares_lifecycle(platform: DesiredPlatform) -> bool:
    if platform.lifecycle_actions:
        return True
    # device_type_overrides may declare lifecycle_actions the base platform
    # omits (e.g. an emulator override's release_forwarded_ports).
    return any(
        isinstance(override, dict) and override.get("lifecycle_actions")
        for override in platform.device_type_overrides.values()
    )


def declared_adapter_hooks(pack: DesiredPack) -> list[str]:
    """Adapter hooks the pack's manifest declarations require (the
    declare-it-then-implement-it rule)."""
    declares_lifecycle = any(_platform_declares_lifecycle(platform) for platform in pack.platforms)
    return ["lifecycle_action"] if declares_lifecycle else []


def missing_declared_hooks(pack: DesiredPack, handle: WorkerHandle) -> list[str]:
    return [hook for hook in declared_adapter_hooks(pack) if not adapter_supports(handle, hook)]


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


async def _call_hook[T](handle: WorkerHandle, hook: str, payload: dict[str, Any], expected: type[T]) -> T:
    try:
        result = await handle.call(hook, payload)
    except AdapterHookTimeoutError, AdapterHookExecutionError, AdapterContractError:
        raise
    except Exception as exc:
        raise AdapterHookExecutionError(hook, handle.pack_id, handle.release, exc) from exc
    if not isinstance(result, expected):
        raise AdapterContractError(
            hook, handle.pack_id, handle.release, f"expected {expected.__name__}, got {type(result).__name__}"
        )
    return result


async def dispatch_discover(handle: WorkerHandle, ctx: DiscoveryContext) -> list[DiscoveryCandidate]:
    return await _call_hook(handle, "discover", {"ctx": _context_payload(ctx)}, list)


async def dispatch_doctor(handle: WorkerHandle, ctx: DoctorContext) -> list[DoctorCheckResult]:
    return await _call_hook(handle, "doctor", {"ctx": _context_payload(ctx)}, list)


async def dispatch_health_check(handle: WorkerHandle, ctx: HealthContext) -> list[HealthCheckResult]:
    return await _call_hook(handle, "health_check", {"ctx": _context_payload(ctx)}, list)


async def dispatch_lifecycle_action(
    handle: WorkerHandle,
    action_id: str,
    args: dict[str, Any],
    ctx: LifecycleContext,
) -> LifecycleActionResult:
    return await _call_hook(
        handle,
        "lifecycle_action",
        {"action_id": action_id, "args": args, "ctx": _context_payload(ctx)},
        LifecycleActionResult,
    )


async def dispatch_pre_session(handle: WorkerHandle, spec: SessionSpec) -> dict[str, Any]:
    return await _call_hook(handle, "pre_session", {"spec": dataclasses.asdict(spec)}, dict)


async def dispatch_post_session(
    handle: WorkerHandle,
    spec: SessionSpec,
    outcome: SessionOutcome,
) -> None:
    await _call_hook(
        handle,
        "post_session",
        {"spec": dataclasses.asdict(spec), "outcome": dataclasses.asdict(outcome)},
        type(None),
    )


async def dispatch_normalize_device(
    handle: WorkerHandle,
    ctx: NormalizeDeviceContext,
) -> NormalizedDevice:
    return await _call_hook(handle, "normalize_device", {"ctx": _context_payload(ctx)}, NormalizedDevice)


async def dispatch_telemetry(handle: WorkerHandle, ctx: TelemetryContext) -> HardwareTelemetry:
    return await _call_hook(handle, "telemetry", {"ctx": _context_payload(ctx)}, HardwareTelemetry)
