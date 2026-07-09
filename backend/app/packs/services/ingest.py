from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import tarfile
import uuid
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import yaml
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.events.models import SystemEvent
from app.packs.manifest import ManifestValidationError, load_manifest_yaml
from app.packs.models import (
    DriverPack,
    DriverPackPlatform,
    DriverPackRelease,
    PackState,
)
from app.packs.services.start_shim import has_session_discovery
from app.packs.services.storage import PackStorageError

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.packs.manifest import Manifest
    from app.packs.services.storage import PackStorageService, StorageRecord


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


def _store_artifact(storage: PackStorageService, *, pack_id: str, release: str, data: bytes) -> StorageRecord:
    return storage.store(pack_id=pack_id, release=release, data=data)


def _add_release_children(session: AsyncSession, manifest: Manifest, release_row: DriverPackRelease) -> None:
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
                data=platform.model_dump(exclude_none=True, mode="json"),
            )
        )


async def record_pack_upload(
    session: AsyncSession,
    *,
    username: str,
    pack_id: str,
    release: str,
    artifact_sha256: str,
    origin_filename: str,
) -> None:
    event = SystemEvent(
        event_id=str(uuid.uuid4()),
        type="driver_pack.upload",
        data={
            "uploaded_by": username,
            "pack_id": pack_id,
            "release": release,
            "artifact_sha256": artifact_sha256,
            "origin_filename": origin_filename,
        },
    )
    session.add(event)


async def ingest_pack_tarball(
    session: AsyncSession,
    *,
    storage: PackStorageService,
    username: str,
    origin_filename: str,
    data: bytes,
) -> DriverPack:
    manifest_text = await asyncio.to_thread(_extract_manifest_text, data)
    try:
        manifest = load_manifest_yaml(manifest_text)
    except ManifestValidationError as exc:
        raise PackIngestValidationError(str(exc)) from exc

    payload_sha = hashlib.sha256(data).hexdigest()
    pack_id = manifest.id
    release_id = manifest.release

    # Orphan-session reaping (session_sync's _kill_orphans) depends on Appium's
    # session_discovery insecure feature to enumerate live sessions; a pack that
    # does not request it would silently disable that reaping path. Shared
    # predicate with the start_shim dispatch injection so the two layers cannot
    # diverge (wave-5 re-review B7).
    discovery_present = has_session_discovery(manifest.insecure_features)
    if not discovery_present:
        logger.warning(
            "pack_ingest_missing_session_discovery pack=%s release=%s: insecure_features lacks a "
            "':session_discovery' entry; injecting '*:session_discovery' into the stored manifest",
            pack_id,
            release_id,
        )

    existing = (
        await session.execute(
            select(DriverPack)
            .where(DriverPack.id == pack_id)
            .options(
                selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms),
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
                            record = await asyncio.to_thread(
                                _store_artifact, storage, pack_id=pack_id, release=release_id, data=data
                            )
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
            display_name=manifest.display_name,
            maintainer=manifest.maintainer or "",
            license=manifest.license or "",
            state=PackState.enabled,
            runtime_policy={"strategy": "recommended"},
        )
        session.add(pack)
        await session.flush()

    try:
        record = await asyncio.to_thread(_store_artifact, storage, pack_id=pack_id, release=release_id, data=data)
    except PackStorageError as exc:
        raise PackIngestConflictError(str(exc)) from exc

    manifest_dict = yaml.safe_load(manifest_text)
    if not isinstance(manifest_dict, dict):
        raise PackIngestValidationError("manifest.yaml must parse to a dictionary")

    # Canonicalize session_discovery into the STORED manifest (wave-5 #29) so it
    # cannot disagree with what dispatch actually runs. start_shim keeps its own
    # injection as the compat layer for packs ingested before this canonicalization.
    if not discovery_present:
        manifest_dict["insecure_features"] = [*(manifest_dict.get("insecure_features") or []), "*:session_discovery"]

    release_row = DriverPackRelease(
        pack_id=pack_id,
        release=release_id,
        manifest_json=manifest_dict,
        artifact_sha256=record.sha256,
        artifact_path=record.path,
    )
    session.add(release_row)
    pack.current_release = release_id
    await session.flush()

    _add_release_children(session, manifest, release_row)
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
            )
        )
    ).scalar_one()
