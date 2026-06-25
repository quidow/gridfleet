from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.devices.models import Device
from app.packs.models import DriverPack, DriverPackRelease, HostPackInstallation
from app.packs.schemas import PackReleaseOut, PackReleasesOut
from app.packs.services.export import _read_artifact, _synthesise_tarball
from app.packs.services.ingest import ingest_pack_tarball
from app.packs.services.release_ordering import parse_release_key, selected_release
from app.packs.services.storage import PackStorageError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.packs.services.storage import PackStorageService


class PackReleaseService:
    def __init__(self, *, storage: PackStorageService) -> None:
        self._storage = storage

    async def list_releases(self, db: AsyncSession, pack_id: str) -> PackReleasesOut | None:
        pack = (
            await db.execute(
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

    async def delete_release(self, db: AsyncSession, pack_id: str, release: str) -> None:
        pack = (
            await db.execute(
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

        installed_count = await db.scalar(
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
            device_count = await db.scalar(
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
        await db.delete(target)
        await db.flush()
        if pack.current_release == release:
            remaining = [row for row in pack.releases if row.id != target.id]
            next_current = selected_release(remaining)
            pack.current_release = next_current.release if next_current is not None else None
            await db.flush()
        if artifact_path:
            Path(artifact_path).unlink(missing_ok=True)

    async def set_current_release(self, db: AsyncSession, pack_id: str, release: str) -> DriverPack:
        pack = (
            await db.execute(
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
        await db.flush()
        return pack

    async def upload(
        self,
        db: AsyncSession,
        *,
        username: str,
        origin_filename: str,
        data: bytes,
    ) -> DriverPack:
        return await ingest_pack_tarball(
            db,
            storage=self._storage,
            username=username,
            origin_filename=origin_filename,
            data=data,
        )

    async def export(self, db: AsyncSession, pack_id: str, release: str) -> tuple[bytes, str]:
        row = (
            await db.execute(
                select(DriverPackRelease).where(
                    DriverPackRelease.pack_id == pack_id,
                    DriverPackRelease.release == release,
                )
            )
        ).scalar_one_or_none()

        if row is None:
            raise LookupError(f"pack {pack_id!r} release {release!r} not found")

        if row.artifact_path is not None:
            try:
                data = await asyncio.to_thread(_read_artifact, self._storage, row.artifact_path)
            except PackStorageError as exc:
                raise LookupError(f"artifact for pack {pack_id!r} release {release!r} is not readable: {exc}") from exc
        else:
            data = await asyncio.to_thread(_synthesise_tarball, row.manifest_json)

        sha256 = hashlib.sha256(data).hexdigest()
        return data, sha256
