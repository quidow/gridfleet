from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import yaml
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.packs.manifest import ManifestValidationError, load_manifest_yaml
from app.packs.models import (
    DriverPack,
    DriverPackFeature,
    DriverPackPlatform,
    DriverPackRelease,
    PackState,
)
from app.packs.services.audit import record_pack_upload
from app.packs.services.storage import PackStorageError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.packs.services.storage import PackStorageService


class PackIngestValidationError(ValueError):
    """Tarball or manifest validation failed."""


class PackIngestConflictError(ValueError):
    """An existing release with same id+release exists with different content."""


MAX_PACK_TARBALL_BYTES = 50 * 1024 * 1024
MAX_PACK_MANIFEST_BYTES = 1024 * 1024
MAX_PACK_TARBALL_MEMBERS = 128
MAX_PACK_UNCOMPRESSED_BYTES = 100 * 1024 * 1024


def _safe_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise PackIngestValidationError(f"unsafe archive path: {name!r}")
    return path


def _validate_archive_member(member: tarfile.TarInfo) -> PurePosixPath:
    path = _safe_archive_path(member.name)
    if not member.isfile() and not member.isdir():
        raise PackIngestValidationError(f"unsupported archive member: {member.name!r}")
    if member.size < 0:
        raise PackIngestValidationError(f"invalid archive member size: {member.name!r}")
    return path


def _extract_limited_manifest(tar: tarfile.TarFile, member: tarfile.TarInfo) -> str:
    if not member.isfile():
        raise PackIngestValidationError("manifest.yaml must be a regular file")
    if member.size > MAX_PACK_MANIFEST_BYTES:
        raise PackIngestValidationError(f"manifest.yaml exceeds maximum size of {MAX_PACK_MANIFEST_BYTES} bytes")
    handle = tar.extractfile(member)
    if handle is None:
        raise PackIngestValidationError("manifest.yaml present but not extractable")
    with handle:
        raw = handle.read(MAX_PACK_MANIFEST_BYTES + 1)
    if len(raw) > MAX_PACK_MANIFEST_BYTES:
        raise PackIngestValidationError(f"manifest.yaml exceeds maximum size of {MAX_PACK_MANIFEST_BYTES} bytes")
    return raw.decode("utf-8")


def _extract_manifest_text(data: bytes) -> str:
    if len(data) > MAX_PACK_TARBALL_BYTES:
        raise PackIngestValidationError(f"tarball exceeds maximum size of {MAX_PACK_TARBALL_BYTES} bytes")

    manifest_text: str | None = None
    member_count = 0
    total_uncompressed = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            while True:
                member = tar.next()
                if member is None:
                    break
                member_count += 1
                if member_count > MAX_PACK_TARBALL_MEMBERS:
                    raise PackIngestValidationError(f"too many archive members; maximum is {MAX_PACK_TARBALL_MEMBERS}")
                member_path = _validate_archive_member(member)
                if member.isfile():
                    total_uncompressed += member.size
                    if total_uncompressed > MAX_PACK_UNCOMPRESSED_BYTES:
                        raise PackIngestValidationError(
                            f"archive uncompressed size exceeds maximum of {MAX_PACK_UNCOMPRESSED_BYTES} bytes"
                        )
                if member_path == PurePosixPath("manifest.yaml"):
                    manifest_text = _extract_limited_manifest(tar, member)
    except tarfile.TarError as exc:
        raise PackIngestValidationError(f"invalid tarball: {exc}") from exc
    if manifest_text is not None:
        return manifest_text
    raise PackIngestValidationError("tarball is missing manifest.yaml at archive root")


async def ingest_pack_tarball(
    session: AsyncSession,
    *,
    storage: PackStorageService,
    username: str,
    origin_filename: str,
    data: bytes,
) -> DriverPack:
    manifest_text = _extract_manifest_text(data)
    try:
        manifest = load_manifest_yaml(manifest_text)
    except ManifestValidationError as exc:
        raise PackIngestValidationError(str(exc)) from exc

    payload_sha = hashlib.sha256(data).hexdigest()
    pack_id = manifest.id
    release_id = manifest.release

    existing = (
        await session.execute(
            select(DriverPack)
            .where(DriverPack.id == pack_id)
            .options(
                selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms),
                selectinload(DriverPack.releases).selectinload(DriverPackRelease.features),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        for release in existing.releases:
            if release.release == release_id:
                if release.artifact_sha256 == payload_sha:
                    existing.current_release = release_id
                    if release.artifact_path is None or not Path(release.artifact_path).is_file():
                        try:
                            record = storage.store(pack_id=pack_id, release=release_id, data=data)
                        except PackStorageError as exc:
                            raise PackIngestConflictError(str(exc)) from exc
                        release.artifact_path = record.path
                        release.artifact_sha256 = record.sha256
                        await session.flush()
                        await record_pack_upload(
                            session,
                            username=username,
                            pack_id=pack_id,
                            release=release_id,
                            artifact_sha256=record.sha256,
                            origin_filename=origin_filename,
                        )
                    return existing
                raise PackIngestConflictError(
                    f"pack {pack_id!r} release {release_id!r} already exists with different content"
                )
        pack = existing
    else:
        pack = DriverPack(
            id=pack_id,
            origin="uploaded",
            display_name=manifest.display_name,
            maintainer=manifest.maintainer or "",
            license=manifest.license or "",
            state=PackState.enabled,
            runtime_policy={"strategy": "recommended"},
        )
        session.add(pack)
        await session.flush()

    try:
        record = storage.store(pack_id=pack_id, release=release_id, data=data)
    except PackStorageError as exc:
        raise PackIngestConflictError(str(exc)) from exc

    manifest_dict = yaml.safe_load(manifest_text)
    if not isinstance(manifest_dict, dict):
        raise PackIngestValidationError("manifest.yaml must parse to a dictionary")

    release_row = DriverPackRelease(
        pack_id=pack_id,
        release=release_id,
        manifest_json=manifest_dict,
        artifact_sha256=record.sha256,
        artifact_path=record.path,
        derived_from_pack_id=manifest.derived_from.pack_id if manifest.derived_from else None,
        derived_from_release=manifest.derived_from.release if manifest.derived_from else None,
        template_id=manifest.template_id,
    )
    session.add(release_row)
    pack.current_release = release_id
    await session.flush()

    for platform in manifest.platforms:
        session.add(
            DriverPackPlatform(
                pack_release_id=release_row.id,
                manifest_platform_id=platform.id,
                display_name=platform.display_name,
                automation_name=platform.automation_name,
                appium_platform_name=platform.appium_platform_name,
                device_types=list(platform.device_types),
                connection_types=list(platform.connection_types),
                grid_slots=list(platform.grid_slots),
                data=platform.model_dump(exclude_none=True, mode="json"),
            )
        )

    for feature_id, feature_data in manifest.features.items():
        session.add(
            DriverPackFeature(
                pack_release_id=release_row.id,
                manifest_feature_id=feature_id,
                data=feature_data.model_dump(exclude_none=True, mode="json"),
            )
        )
    await session.flush()

    await record_pack_upload(
        session,
        username=username,
        pack_id=pack_id,
        release=release_id,
        artifact_sha256=record.sha256,
        origin_filename=origin_filename,
    )

    return (
        await session.execute(
            select(DriverPack)
            .where(DriverPack.id == pack_id)
            .options(
                selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms),
                selectinload(DriverPack.releases).selectinload(DriverPackRelease.features),
            )
        )
    ).scalar_one()
