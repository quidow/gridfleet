from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.events.event_bus import build_event, stage_system_event
from app.packs.manifest import ManifestValidationError, load_manifest_yaml
from app.packs.models import (
    DriverPack,
    DriverPackPlatform,
    DriverPackRelease,
    PackState,
)
from app.packs.services.artifact_ledger import activate_artifact, reserve_artifact
from app.packs.services.service import build_pack_out
from app.packs.services.start_shim import has_session_discovery
from app.packs.services.storage import PackStorageError

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SessionFactory
    from app.packs.manifest import Manifest
    from app.packs.schemas import PackOut
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
    stage_system_event(
        session,
        build_event(
            "driver_pack.upload",
            {
                "uploaded_by": username,
                "pack_id": pack_id,
                "release": release,
                "artifact_sha256": artifact_sha256,
                "origin_filename": origin_filename,
            },
        ),
    )


@dataclass(frozen=True, slots=True)
class ParsedPack:
    """Everything the transaction phases need, derived from the bytes alone."""

    manifest: Manifest
    manifest_dict: dict[str, Any]
    pack_id: str
    release: str
    payload_sha: str


@dataclass(frozen=True, slots=True)
class ArtifactReservation:
    """The path the upload claimed, and whether its bytes still have to be written."""

    artifact_path: str
    needs_write: bool


def parse_pack_tarball(data: bytes) -> ParsedPack:
    """Validate the tarball and canonicalize its manifest. Blocking; no session."""
    manifest_text = _extract_manifest_text(data)
    try:
        manifest = load_manifest_yaml(manifest_text)
    except ManifestValidationError as exc:
        raise PackIngestValidationError(str(exc)) from exc

    manifest_dict = yaml.safe_load(manifest_text)
    if not isinstance(manifest_dict, dict):
        raise PackIngestValidationError("manifest.yaml must parse to a dictionary")

    if not has_session_discovery(manifest.insecure_features):
        logger.warning(
            "pack_ingest_missing_session_discovery pack=%s release=%s: insecure_features lacks a "
            "':session_discovery' entry; injecting '*:session_discovery' into the stored manifest",
            manifest.id,
            manifest.release,
        )
        manifest_dict["insecure_features"] = [*(manifest_dict.get("insecure_features") or []), "*:session_discovery"]

    return ParsedPack(
        manifest=manifest,
        manifest_dict=manifest_dict,
        pack_id=manifest.id,
        release=manifest.release,
        payload_sha=hashlib.sha256(data).hexdigest(),
    )


async def reserve_pack_upload(
    session: AsyncSession,
    *,
    storage: PackStorageService,
    parsed: ParsedPack,
) -> ArtifactReservation:
    """Phase 1: settle the conflict question and claim the artifact path."""
    existing = (
        await session.execute(
            select(DriverPackRelease).where(
                DriverPackRelease.pack_id == parsed.pack_id,
                DriverPackRelease.release == parsed.release,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.artifact_sha256 != parsed.payload_sha:
            raise PackIngestConflictError(
                f"pack {parsed.pack_id!r} release {parsed.release!r} already exists with different content"
            )
        if existing.artifact_path is not None and Path(existing.artifact_path).is_file():
            return ArtifactReservation(artifact_path=existing.artifact_path, needs_write=False)

    artifact_path = storage.path_for(pack_id=parsed.pack_id, release=parsed.release)
    await reserve_artifact(session, path=artifact_path)
    return ArtifactReservation(artifact_path=artifact_path, needs_write=True)


async def activate_pack_upload(
    session: AsyncSession,
    *,
    parsed: ParsedPack,
    record: StorageRecord | None,
    username: str,
    origin_filename: str,
) -> PackOut:
    """Phase 3: land the metadata and the ledger promotion in one transaction."""
    pack = (
        await session.execute(
            select(DriverPack)
            .where(DriverPack.id == parsed.pack_id)
            .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms))
        )
    ).scalar_one_or_none()
    if pack is None:
        pack = DriverPack(
            id=parsed.pack_id,
            display_name=parsed.manifest.display_name,
            maintainer=parsed.manifest.maintainer or "",
            license=parsed.manifest.license or "",
            state=PackState.enabled,
            runtime_policy={"strategy": "recommended"},
        )
        session.add(pack)
        await session.flush()
        release_row = None
    else:
        release_row = next((row for row in pack.releases if row.release == parsed.release), None)
    if release_row is None:
        release_row = DriverPackRelease(
            pack_id=parsed.pack_id,
            release=parsed.release,
            manifest_json=parsed.manifest_dict,
            artifact_sha256=record.sha256 if record is not None else None,
            artifact_path=record.path if record is not None else None,
        )
        session.add(release_row)
        await session.flush()
        _add_release_children(session, parsed.manifest, release_row)
    elif record is not None:
        release_row.artifact_path = record.path
        release_row.artifact_sha256 = record.sha256

    pack.current_release = parsed.release
    await session.flush()

    if record is not None:
        await activate_artifact(session, path=record.path, sha256=record.sha256, size_bytes=record.size)
        await record_pack_upload(
            session,
            username=username,
            pack_id=parsed.pack_id,
            release=parsed.release,
            artifact_sha256=record.sha256,
            origin_filename=origin_filename,
        )

    return build_pack_out(
        (
            await session.execute(
                select(DriverPack)
                .where(DriverPack.id == parsed.pack_id)
                .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms))
            )
        ).scalar_one()
    )


async def ingest_pack_tarball(
    session_factory: SessionFactory,
    *,
    storage: PackStorageService,
    username: str,
    origin_filename: str,
    data: bytes,
) -> PackOut:
    """Reserve, write, activate. Owns both boundaries; the bytes move between them."""
    parsed = await asyncio.to_thread(parse_pack_tarball, data)

    async with session_factory.begin() as session:
        reservation = await reserve_pack_upload(session, storage=storage, parsed=parsed)

    record: StorageRecord | None = None
    if reservation.needs_write:
        try:
            record = await asyncio.to_thread(
                _store_artifact, storage, pack_id=parsed.pack_id, release=parsed.release, data=data
            )
        except PackStorageError as exc:
            raise PackIngestConflictError(str(exc)) from exc

    async with session_factory.begin() as session:
        return await activate_pack_upload(
            session,
            parsed=parsed,
            record=record,
            username=username,
            origin_filename=origin_filename,
        )
