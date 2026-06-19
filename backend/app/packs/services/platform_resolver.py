from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.errors import PackDisabledError, PackDrainingError, PackUnavailableError, PlatformRemovedError
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
    skip_when: dict[str, Any] = field(default_factory=dict)


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
    identity_scheme: str
    identity_scope: str
    capabilities: dict[str, Any]
    default_capabilities: dict[str, Any]
    device_fields_schema: list[dict[str, Any]]
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
                skip_when=dict(p.get("skip_when") or {}),
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
        identity_scheme=identity["scheme"],
        identity_scope=identity["scope"],
        capabilities=dict(platform.data.get("capabilities", {})),
        default_capabilities=dict(
            override.get("default_capabilities") or platform.data.get("default_capabilities", {})
        ),
        device_fields_schema=list(
            override.get("device_fields_schema") or platform.data.get("device_fields_schema", [])
        ),
        lifecycle_actions=list(override.get("lifecycle_actions") or platform.data.get("lifecycle_actions", [])),
        health_checks=list(platform.data.get("health_checks", [])),
        connection_behavior=dict(override.get("connection_behavior") or platform.data.get("connection_behavior", {})),
        parallel_resources=parallel_resources,
    )


def applicable_resource_ports(
    resolved: ResolvedPackPlatform, device_config: dict[str, Any] | None
) -> list[ResolvedParallelResourcePort]:
    """Filter parallel-resource ports by their ``skip_when`` device_config gate.

    A port is skipped when every gated field equals its listed value. An unset
    field falls back to its schema ``default`` (same semantics as
    ``required_for_session_when``), so a field the device type never declares
    can't match the gate.
    """
    cfg = device_config or {}
    fields = resolved.device_fields_schema
    kept: list[ResolvedParallelResourcePort] = []
    for port in resolved.parallel_resources.ports:
        if port.skip_when and all(
            cfg.get(key, next((f.get("default") for f in fields if f.get("id") == key), None)) == expected
            for key, expected in port.skip_when.items()
        ):
            continue
        kept.append(port)
    return kept


async def assert_runnable(
    session: AsyncSession,
    *,
    pack_id: str,
    platform_id: str,
    pack_lock: bool = False,
) -> ResolvedPackPlatform:
    # ``pack_lock=True`` callers (e.g. ``create_run`` before inserting a
    # ``DeviceReservation``) take ``SELECT … FOR SHARE`` on the pack row so
    # they conflict with ``try_complete_drain``'s ``SELECT … FOR UPDATE``.
    # Without this, an in-flight allocator that observed ``state=enabled``
    # could commit a reservation after the drain transition flipped state
    # to ``draining`` and the drain-completion path would not see the new
    # reservation.
    stmt = select(DriverPack).where(DriverPack.id == pack_id)
    if pack_lock:
        stmt = stmt.with_for_update(read=True)
    pack = (await session.execute(stmt)).scalar_one_or_none()
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


def evaluate_runnable(pack: DriverPack | None, *, platform_id: str | None) -> str | None:
    """Pure, no-DB equivalent of :func:`assert_runnable`'s reachability checks.

    Given an already-loaded *pack* (with ``releases`` and their ``platforms`` eager
    loaded), return the blocking error ``code`` that :func:`assert_runnable` would
    raise — or ``None`` when the pack/platform is runnable. Used by the batched
    device-list serialization path to avoid one ``assert_runnable`` query per device.
    """
    if pack is None:
        return PackUnavailableError.code
    if pack.state == PackState.disabled:
        return PackDisabledError.code
    if pack.state == PackState.draining:
        return PackDrainingError.code
    if pack.state != PackState.enabled:
        return PackDisabledError.code
    release = selected_release(pack.releases, pack.current_release)
    platform = (
        next((row for row in release.platforms if row.manifest_platform_id == platform_id), None)
        if release is not None
        else None
    )
    if release is None or platform is None:
        return PlatformRemovedError.code
    return None


def _device_type_override(platform_data: dict[str, Any], device_type: str | None) -> dict[str, Any]:
    if not device_type:
        return {}
    overrides = platform_data.get("device_type_overrides") or {}
    override = overrides.get(device_type)
    return override if isinstance(override, dict) else {}
