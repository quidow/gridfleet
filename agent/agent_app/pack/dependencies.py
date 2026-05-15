"""FastAPI dependencies for ``/agent/pack/*`` routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import Depends, Query, Request

from agent_app.error_codes import AgentErrorCode, http_exc
from agent_app.pack.constants import PACK_ID_PATTERN, PLATFORM_ID_PATTERN
from agent_app.pack.manifest import resolve_desired_platform

if TYPE_CHECKING:
    from agent_app.pack.adapter_registry import AdapterRegistry
    from agent_app.pack.manifest import DesiredPlatform
    from agent_app.pack.state import PackStateLoop


def _latest_desired(request: Request) -> list[Any]:
    loop = getattr(request.app.state, "pack_state_loop", None)
    return list(loop.latest_desired_packs or []) if loop else []


def _release_for_pack(request: Request, pack_id: str) -> str | None:
    for pack in _latest_desired(request):
        if getattr(pack, "id", None) == pack_id:
            return str(getattr(pack, "release", ""))
    return None


def _optional_adapter_registry(request: Request) -> AdapterRegistry | None:
    return cast("AdapterRegistry | None", getattr(request.app.state, "adapter_registry", None))


def _require_adapter_registry(request: Request) -> AdapterRegistry:
    registry = cast("AdapterRegistry | None", getattr(request.app.state, "adapter_registry", None))
    if registry is None:
        raise http_exc(
            status_code=404,
            code=AgentErrorCode.NO_ADAPTER,
            message="No adapter registry available",
        )
    return registry


def _host_id(request: Request) -> str:
    host_identity = getattr(request.app.state, "host_identity", None)
    if host_identity is None:
        return ""
    value = host_identity.get()
    return value or ""


def _pack_state_loop(request: Request) -> PackStateLoop | None:
    return cast("PackStateLoop | None", getattr(request.app.state, "pack_state_loop", None))


def _desired_platform(
    pack_id: Annotated[str, Query(min_length=1, pattern=PACK_ID_PATTERN)],
    platform_id: Annotated[str, Query(min_length=1, pattern=PLATFORM_ID_PATTERN)],
    latest_desired: Annotated[list[Any], Depends(_latest_desired)],
) -> tuple[DesiredPlatform, str]:
    platform_def = resolve_desired_platform(latest_desired, pack_id=pack_id, platform_id=platform_id)
    if platform_def is None:
        raise http_exc(
            status_code=404,
            code=AgentErrorCode.UNKNOWN_PLATFORM,
            message=f"Unknown desired pack platform {pack_id}:{platform_id}",
        )
    release: str | None = None
    for pack in latest_desired:
        if getattr(pack, "id", None) == pack_id:
            release = str(getattr(pack, "release", ""))
            break
    if not release:
        raise http_exc(
            status_code=404,
            code=AgentErrorCode.UNKNOWN_PLATFORM,
            message=f"Unknown pack release for {pack_id}",
        )
    return platform_def, release


LatestDesiredDep = Annotated[list[Any], Depends(_latest_desired)]
OptionalAdapterRegistryDep = Annotated["AdapterRegistry | None", Depends(_optional_adapter_registry)]
AdapterRegistryDep = Annotated["AdapterRegistry", Depends(_require_adapter_registry)]
HostIdDep = Annotated[str, Depends(_host_id)]
PackStateLoopDep = Annotated["PackStateLoop | None", Depends(_pack_state_loop)]
DesiredPlatformDep = Annotated[tuple["DesiredPlatform", str], Depends(_desired_platform)]
