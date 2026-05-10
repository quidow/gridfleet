"""Pack adapter dispatch helpers for discovery, health, lifecycle, session, normalize, and telemetry hooks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_app.pack.adapter_dispatch import (
    dispatch_health_check as _dispatch_health,
)
from agent_app.pack.adapter_dispatch import (
    dispatch_lifecycle_action as _dispatch_lifecycle,
)
from agent_app.pack.adapter_dispatch import (
    dispatch_normalize_device as _dispatch_normalize,
)
from agent_app.pack.adapter_dispatch import (
    dispatch_post_session as _dispatch_post_session,
)
from agent_app.pack.adapter_dispatch import (
    dispatch_pre_session as _dispatch_pre_session,
)
from agent_app.pack.adapter_dispatch import (
    dispatch_telemetry as _dispatch_telemetry,
)
from agent_app.pack.adapter_types import (
    HealthCheckResult,
    LifecycleActionResult,
    SessionOutcome,
    SessionSpec,
)

if TYPE_CHECKING:
    from agent_app.pack.adapter_registry import AdapterRegistry

__all__ = [
    "adapter_health_check",
    "adapter_lifecycle_action",
    "adapter_normalize_device",
    "adapter_post_session",
    "adapter_pre_session",
    "adapter_telemetry",
]


class _HealthCtx:
    def __init__(
        self,
        identity_value: str,
        allow_boot: bool,
        platform_id: str | None,
        device_type: str | None,
        connection_type: str | None,
        ip_address: str | None = None,
        ip_ping_timeout_sec: float | None = None,
        ip_ping_count: int | None = None,
    ) -> None:
        self.device_identity_value = identity_value
        self.allow_boot = allow_boot
        self.platform_id = platform_id
        self.device_type = device_type
        self.connection_type = connection_type
        self.ip_address = ip_address
        self.ip_ping_timeout_sec = ip_ping_timeout_sec
        self.ip_ping_count = ip_ping_count


class _LifecycleCtx:
    def __init__(self, host_id: str, identity_value: str) -> None:
        self.host_id = host_id
        self.device_identity_value = identity_value


class _NormalizeCtx:
    def __init__(self, host_id: str, platform_id: str, raw_input: dict[str, Any]) -> None:
        self.host_id = host_id
        self.platform_id = platform_id
        self.raw_input = raw_input


class _TelemetryCtx:
    def __init__(self, device_identity_value: str, connection_target: str) -> None:
        self.device_identity_value = device_identity_value
        self.connection_target = connection_target


def _adapter_health_payload(results: list[HealthCheckResult]) -> dict[str, Any]:
    return {
        "healthy": all(r.ok for r in results),
        "checks": [{"check_id": r.check_id, "ok": r.ok, "message": r.detail} for r in results],
    }


def _adapter_lifecycle_payload(result: LifecycleActionResult) -> dict[str, Any]:
    return {"success": result.ok, "state": result.state, "detail": result.detail}


async def adapter_health_check(
    *,
    adapter_registry: AdapterRegistry,
    pack_id: str,
    pack_release: str,
    identity_value: str,
    allow_boot: bool,
    platform_id: str | None = None,
    device_type: str | None = None,
    connection_type: str | None = None,
    ip_address: str | None = None,
    ip_ping_timeout_sec: float | None = None,
    ip_ping_count: int | None = None,
) -> dict[str, Any] | None:
    """Dispatch through the loaded adapter; return ``None`` if not available."""

    adapter = adapter_registry.get(pack_id, pack_release)
    if adapter is None:
        return None
    ctx = _HealthCtx(
        identity_value=identity_value,
        allow_boot=allow_boot,
        platform_id=platform_id,
        device_type=device_type,
        connection_type=connection_type,
        ip_address=ip_address,
        ip_ping_timeout_sec=ip_ping_timeout_sec,
        ip_ping_count=ip_ping_count,
    )
    results = await _dispatch_health(adapter, ctx)
    return _adapter_health_payload(results)


async def adapter_lifecycle_action(
    *,
    adapter_registry: AdapterRegistry,
    pack_id: str,
    pack_release: str,
    host_id: str,
    identity_value: str,
    action: str,
    args: dict[str, Any],
) -> dict[str, Any] | None:
    """Dispatch through the loaded adapter; return ``None`` if not available."""

    adapter = adapter_registry.get(pack_id, pack_release)
    if adapter is None:
        return None
    ctx = _LifecycleCtx(host_id=host_id, identity_value=identity_value)
    result = await _dispatch_lifecycle(adapter, action, args, ctx)
    return _adapter_lifecycle_payload(result)


async def adapter_pre_session(
    *,
    adapter_registry: AdapterRegistry,
    pack_id: str,
    pack_release: str,
    platform_id: str,
    identity_value: str,
    capabilities: dict[str, Any],
) -> dict[str, Any]:
    """Return adapter-supplied extra caps. Empty dict when no adapter is loaded."""

    adapter = adapter_registry.get(pack_id, pack_release)
    if adapter is None:
        return {}
    spec = SessionSpec(
        pack_id=pack_id,
        platform_id=platform_id,
        device_identity_value=identity_value,
        capabilities=dict(capabilities),
    )
    return await _dispatch_pre_session(adapter, spec)


async def adapter_post_session(
    *,
    adapter_registry: AdapterRegistry,
    pack_id: str,
    pack_release: str,
    platform_id: str,
    identity_value: str,
    ok: bool,
    detail: str = "",
) -> bool:
    """Return ``True`` when the call was dispatched, ``False`` if no adapter."""

    adapter = adapter_registry.get(pack_id, pack_release)
    if adapter is None:
        return False
    spec = SessionSpec(
        pack_id=pack_id,
        platform_id=platform_id,
        device_identity_value=identity_value,
    )
    outcome = SessionOutcome(ok=ok, detail=detail)
    await _dispatch_post_session(adapter, spec, outcome)
    return True


async def adapter_normalize_device(
    *,
    adapter_registry: AdapterRegistry,
    pack_id: str,
    pack_release: str,
    host_id: str,
    platform_id: str,
    raw_input: dict[str, Any],
) -> dict[str, Any] | None:
    """Dispatch device normalization through the loaded adapter."""

    adapter = adapter_registry.get(pack_id, pack_release)
    if adapter is None:
        return None
    ctx = _NormalizeCtx(host_id=host_id, platform_id=platform_id, raw_input=raw_input)
    result = await _dispatch_normalize(adapter, ctx)
    return {
        "identity_scheme": result.identity_scheme,
        "identity_scope": result.identity_scope,
        "identity_value": result.identity_value,
        "connection_target": result.connection_target,
        "ip_address": result.ip_address,
        "device_type": result.device_type,
        "connection_type": result.connection_type,
        "os_version": result.os_version,
        "manufacturer": result.manufacturer,
        "model": result.model,
        "model_number": result.model_number,
        "software_versions": result.software_versions,
        "field_errors": [{"field_id": error.field_id, "message": error.message} for error in result.field_errors],
    }


async def adapter_telemetry(
    *,
    adapter_registry: AdapterRegistry,
    pack_id: str,
    pack_release: str,
    identity_value: str,
    connection_target: str,
) -> dict[str, Any] | None:
    """Dispatch hardware telemetry through the loaded adapter."""

    adapter = adapter_registry.get(pack_id, pack_release)
    if adapter is None:
        return None
    ctx = _TelemetryCtx(device_identity_value=identity_value, connection_target=connection_target)
    result = await _dispatch_telemetry(adapter, ctx)
    if not result.supported:
        return {"support_status": "unsupported"}
    return {
        "support_status": "supported",
        "battery_level_percent": result.battery_level_percent,
        "battery_temperature_c": result.battery_temperature_c,
        "charging_state": result.charging_state,
    }
