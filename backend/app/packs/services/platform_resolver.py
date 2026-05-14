from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.errors import PackDisabledError, PackDrainingError, PackUnavailableError, PlatformRemovedError
from app.packs.models import DriverPack, DriverPackRelease, PackState
from app.packs.services.release_ordering import selected_release

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class PackPlatformNotFound(LookupError):  # noqa: N818
    pass


@dataclass(frozen=True)
class ResolvedParallelResourcePort:
    capability_name: str
    start: int


@dataclass(frozen=True)
class ResolvedParallelResources:
    ports: list[ResolvedParallelResourcePort]
    derived_data_path: bool


@dataclass(frozen=True)
class ResolvedPackPlatform:
    pack_id: str
    release: str
    platform_id: str
    display_name: str
    automation_name: str
    appium_platform_name: str
    device_types: list[str]
    connection_types: list[str]
    grid_slots: list[str]
    identity_scheme: str
    identity_scope: str
    capabilities: dict[str, Any]
    default_capabilities: dict[str, Any]
    device_fields_schema: list[dict[str, Any]]
    host_fields_schema: list[dict[str, Any]]
    lifecycle_actions: list[dict[str, Any]]
    health_checks: list[dict[str, Any]]
    connection_behavior: dict[str, Any]
    parallel_resources: ResolvedParallelResources


async def resolve_pack_platform(
    session: AsyncSession,
    *,
    pack_id: str,
    platform_id: str,
    device_type: str | None = None,
) -> ResolvedPackPlatform:
    pack = await session.scalar(
        select(DriverPack)
        .where(DriverPack.id == pack_id, DriverPack.state == PackState.enabled)
        .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms))
    )
    if pack is None:
        raise PackPlatformNotFound(f"{pack_id}:{platform_id}")

    release = selected_release(pack.releases, pack.current_release)
    platform = (
        next((row for row in release.platforms if row.manifest_platform_id == platform_id), None)
        if release is not None
        else None
    )
    if release is None or platform is None:
        raise PackPlatformNotFound(f"{pack_id}:{platform_id}")
    override = _device_type_override(platform.data, device_type)
    identity = override.get("identity") or platform.data["identity"]
    raw_pr = platform.data.get("parallel_resources", {})
    parallel_resources = ResolvedParallelResources(
        ports=[
            ResolvedParallelResourcePort(
                capability_name=p["capability_name"],
                start=p["start"],
            )
            for p in (raw_pr.get("ports") or [])
        ],
        derived_data_path=bool(raw_pr.get("derived_data_path", False)),
    )
    return ResolvedPackPlatform(
        pack_id=release.pack_id,
        release=release.release,
        platform_id=platform.manifest_platform_id,
        display_name=platform.display_name,
        automation_name=platform.automation_name,
        appium_platform_name=platform.appium_platform_name,
        device_types=list(platform.device_types),
        connection_types=list(platform.connection_types),
        grid_slots=list(platform.grid_slots),
        identity_scheme=identity["scheme"],
        identity_scope=identity["scope"],
        capabilities=dict(platform.data.get("capabilities", {})),
        default_capabilities=dict(
            override.get("default_capabilities") or platform.data.get("default_capabilities", {})
        ),
        device_fields_schema=list(
            override.get("device_fields_schema") or platform.data.get("device_fields_schema", [])
        ),
        host_fields_schema=list(platform.data.get("host_fields_schema", [])),
        lifecycle_actions=list(override.get("lifecycle_actions") or platform.data.get("lifecycle_actions", [])),
        health_checks=list(platform.data.get("health_checks", [])),
        connection_behavior=dict(override.get("connection_behavior") or platform.data.get("connection_behavior", {})),
        parallel_resources=parallel_resources,
    )


async def assert_runnable(
    session: AsyncSession,
    *,
    pack_id: str,
    platform_id: str,
) -> ResolvedPackPlatform:
    pack = await session.scalar(select(DriverPack).where(DriverPack.id == pack_id))
    if pack is None:
        raise PackUnavailableError(pack_id)
    if pack.state == PackState.disabled:
        raise PackDisabledError(pack_id)
    if pack.state == PackState.draining:
        raise PackDrainingError(pack_id)
    if pack.state != PackState.enabled:
        raise PackDisabledError(pack_id)
    try:
        return await resolve_pack_platform(session, pack_id=pack_id, platform_id=platform_id)
    except PackPlatformNotFound as exc:
        raise PlatformRemovedError(pack_id, platform_id) from exc


def _device_type_override(platform_data: dict[str, Any], device_type: str | None) -> dict[str, Any]:
    if not device_type:
        return {}
    overrides = platform_data.get("device_type_overrides") or {}
    override = overrides.get(device_type)
    return override if isinstance(override, dict) else {}
