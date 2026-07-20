from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from agent_app.pack.adapter_dispatch import (
    adapter_supports,
    dispatch_health_check,
    dispatch_lifecycle_action,
    dispatch_normalize_device,
    dispatch_post_session,
    dispatch_pre_session,
)
from agent_app.pack.adapter_types import SessionOutcome, SessionSpec
from agent_app.pack.contexts import LifecycleCtx, NormalizeCtx
from agent_app.pack.router import _adapter_health_payload, _adapter_lifecycle_payload, worker_or_none

if TYPE_CHECKING:
    from agent_app.pack.adapter_registry import AdapterRegistry
    from agent_app.pack.adapter_types import HealthContext


async def adapter_health_check(
    *, adapter_registry: AdapterRegistry, pack_id: str, pack_release: str, ctx: HealthContext
) -> dict[str, Any] | None:
    handle = worker_or_none(adapter_registry, pack_id, pack_release)
    if handle is None or not adapter_supports(handle, "health_check"):
        return None
    return _adapter_health_payload(await dispatch_health_check(handle, ctx))


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
    handle = worker_or_none(adapter_registry, pack_id, pack_release)
    if handle is None or not adapter_supports(handle, "lifecycle_action"):
        return None
    result = await dispatch_lifecycle_action(
        handle,
        action,
        args,
        LifecycleCtx(host_id=host_id, device_identity_value=identity_value),
    )
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
    handle = worker_or_none(adapter_registry, pack_id, pack_release)
    if handle is None or not adapter_supports(handle, "pre_session"):
        return {}
    return await dispatch_pre_session(
        handle,
        SessionSpec(pack_id, platform_id, identity_value, dict(capabilities)),
    )


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
    handle = worker_or_none(adapter_registry, pack_id, pack_release)
    if handle is None or not adapter_supports(handle, "post_session"):
        return False
    await dispatch_post_session(
        handle,
        SessionSpec(pack_id, platform_id, identity_value),
        SessionOutcome(ok, detail),
    )
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
    handle = worker_or_none(adapter_registry, pack_id, pack_release)
    if handle is None or not adapter_supports(handle, "normalize_device"):
        return None
    result = await dispatch_normalize_device(handle, NormalizeCtx(host_id, platform_id, raw_input))
    return dataclasses.asdict(result)
