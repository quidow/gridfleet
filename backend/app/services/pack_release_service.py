from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models.device import Device
from app.models.driver_pack import DriverPack, DriverPackRelease
from app.models.host_pack_installation import HostPackInstallation
from app.schemas.driver_pack import PackReleaseOut, PackReleasesOut
from app.services.pack_release_ordering import parse_release_key, selected_release

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def list_releases(session: AsyncSession, pack_id: str) -> PackReleasesOut | None:
    pack = (
        await session.execute(
            select(DriverPack)
            .where(DriverPack.id == pack_id)
            .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms))
        )
    ).scalar_one_or_none()
    if pack is None:
        return None

    current = selected_release(pack.releases, pack.current_release)
    releases = sorted(pack.releases, key=lambda row: parse_release_key(row.release), reverse=True)
    return PackReleasesOut(
        pack_id=pack.id,
        releases=[
            PackReleaseOut(
                release=release.release,
                is_current=current is not None and release.id == current.id,
                artifact_sha256=release.artifact_sha256,
                created_at=release.created_at,
                platform_ids=[platform.manifest_platform_id for platform in release.platforms],
            )
            for release in releases
        ],
    )


async def delete_release(session: AsyncSession, pack_id: str, release: str) -> None:
    pack = (
        await session.execute(
            select(DriverPack)
            .where(DriverPack.id == pack_id)
            .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms))
        )
    ).scalar_one_or_none()
    if pack is None:
        raise LookupError(f"Pack {pack_id!r} not found")

    target = next((row for row in pack.releases if row.release == release), None)
    if target is None:
        raise LookupError(f"Pack {pack_id!r} release {release!r} not found")

    if len(pack.releases) == 1:
        raise ValueError(f"Cannot delete the only release for pack {pack_id!r}")

    installed_count = await session.scalar(
        select(func.count())
        .select_from(HostPackInstallation)
        .where(
            HostPackInstallation.pack_id == pack_id,
            HostPackInstallation.pack_release == release,
        )
    )
    if installed_count:
        noun = "host" if installed_count == 1 else "hosts"
        raise RuntimeError(f"Cannot delete release {release!r}; it is installed on {installed_count} {noun}")

    remaining_platforms = {
        platform.manifest_platform_id
        for other in pack.releases
        if other.id != target.id
        for platform in other.platforms
    }
    target_platforms = {platform.manifest_platform_id for platform in target.platforms}
    orphaned_platforms = target_platforms - remaining_platforms
    if orphaned_platforms:
        device_count = await session.scalar(
            select(func.count())
            .select_from(Device)
            .where(
                Device.pack_id == pack_id,
                Device.platform_id.in_(sorted(orphaned_platforms)),
            )
        )
        if device_count:
            raise RuntimeError(
                f"Cannot delete release {release!r}; {device_count} device(s) use platform(s) "
                f"only present in that release"
            )

    artifact_path = target.artifact_path
    await session.delete(target)
    await session.flush()
    if pack.current_release == release:
        remaining = [row for row in pack.releases if row.id != target.id]
        next_current = selected_release(remaining)
        pack.current_release = next_current.release if next_current is not None else None
        await session.flush()
    if artifact_path:
        Path(artifact_path).unlink(missing_ok=True)


async def set_current_release(session: AsyncSession, pack_id: str, release: str) -> DriverPack:
    pack = (
        await session.execute(
            select(DriverPack)
            .where(DriverPack.id == pack_id)
            .options(
                selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms),
                selectinload(DriverPack.releases).selectinload(DriverPackRelease.features),
            )
        )
    ).scalar_one_or_none()
    if pack is None:
        raise LookupError(f"Pack {pack_id!r} not found")
    if not any(row.release == release for row in pack.releases):
        raise LookupError(f"Pack {pack_id!r} release {release!r} not found")
    pack.current_release = release
    await session.flush()
    return pack
