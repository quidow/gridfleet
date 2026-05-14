from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from app.packs.models import PackState
from app.packs.services.storage import PackStorageService
from app.packs.services.upload import (
    MAX_PACK_MANIFEST_BYTES,
    MAX_PACK_TARBALL_BYTES,
    MAX_PACK_TARBALL_MEMBERS,
    PackUploadConflictError,
    PackUploadValidationError,
    upload_pack,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_MANIFEST = """\
schema_version: 1
id: vendor-foo
release: 0.1.0
display_name: Vendor Foo
appium_server:
  source: npm
  package: appium
  version: ">=2.5,<3"
  recommended: 2.19.0
appium_driver:
  source: npm
  package: appium-vendor-foo-driver
  version: ">=0,<1"
  recommended: 0.1.0
platforms:
  - id: vendor_p
    display_name: Vendor Platform
    automation_name: VendorAutomation
    appium_platform_name: Vendor
    device_types: [real_device]
    connection_types: [network]
    grid_slots: [native]
    capabilities: { stereotype: {}, session_required: [] }
    identity: { scheme: vendor_uid, scope: global }
"""


def _build_tarball(manifest: str = _MANIFEST, extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        manifest_bytes = manifest.encode()
        info = tarfile.TarInfo(name="manifest.yaml")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))
        for path, content in (extra or {}).items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _build_tarball_with_symlink() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        manifest_bytes = _MANIFEST.encode()
        info = tarfile.TarInfo(name="manifest.yaml")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))
        link = tarfile.TarInfo(name="adapter/bad.whl")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../bad.whl"
        tar.addfile(link)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_upload_persists_pack_and_writes_audit(db_session: AsyncSession, tmp_path: Path) -> None:
    storage = PackStorageService(root=tmp_path)
    tarball = _build_tarball()
    pack = await upload_pack(
        db_session,
        storage=storage,
        username="alice",
        origin_filename="vendor-foo-0.1.0.tar.gz",
        data=tarball,
    )
    await db_session.flush()
    assert pack.id == "vendor-foo"
    assert pack.origin == "uploaded"
    assert pack.state == PackState.enabled
    release = pack.releases[0]
    assert release.release == "0.1.0"
    assert release.artifact_sha256 is not None and len(release.artifact_sha256) == 64
    assert release.artifact_path is not None
    assert Path(release.artifact_path).read_bytes() == tarball


@pytest.mark.asyncio
async def test_upload_rejects_missing_manifest(db_session: AsyncSession, tmp_path: Path) -> None:
    storage = PackStorageService(root=tmp_path)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="readme.md")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"hi\n\n"))
    with pytest.raises(PackUploadValidationError, match=r"manifest\.yaml"):
        await upload_pack(
            db_session, storage=storage, username="alice", origin_filename="x.tar.gz", data=buf.getvalue()
        )


@pytest.mark.asyncio
async def test_upload_rejects_oversized_tarball(db_session: AsyncSession, tmp_path: Path) -> None:
    storage = PackStorageService(root=tmp_path)
    with pytest.raises(PackUploadValidationError, match="tarball exceeds maximum size"):
        await upload_pack(
            db_session,
            storage=storage,
            username="alice",
            origin_filename="x.tar.gz",
            data=b"x" * (MAX_PACK_TARBALL_BYTES + 1),
        )


@pytest.mark.asyncio
async def test_upload_rejects_oversized_manifest(db_session: AsyncSession, tmp_path: Path) -> None:
    storage = PackStorageService(root=tmp_path)
    oversized_manifest = "x" * (MAX_PACK_MANIFEST_BYTES + 1)
    with pytest.raises(PackUploadValidationError, match=r"manifest\.yaml exceeds maximum size"):
        await upload_pack(
            db_session,
            storage=storage,
            username="alice",
            origin_filename="x.tar.gz",
            data=_build_tarball(manifest=oversized_manifest),
        )


@pytest.mark.asyncio
async def test_upload_rejects_too_many_archive_members(db_session: AsyncSession, tmp_path: Path) -> None:
    storage = PackStorageService(root=tmp_path)
    extra = {f"extra-{index}.txt": b"x" for index in range(MAX_PACK_TARBALL_MEMBERS)}
    with pytest.raises(PackUploadValidationError, match="too many archive members"):
        await upload_pack(
            db_session,
            storage=storage,
            username="alice",
            origin_filename="x.tar.gz",
            data=_build_tarball(extra=extra),
        )


@pytest.mark.asyncio
async def test_upload_rejects_archive_links(db_session: AsyncSession, tmp_path: Path) -> None:
    storage = PackStorageService(root=tmp_path)
    with pytest.raises(PackUploadValidationError, match="unsupported archive member"):
        await upload_pack(
            db_session,
            storage=storage,
            username="alice",
            origin_filename="x.tar.gz",
            data=_build_tarball_with_symlink(),
        )


@pytest.mark.asyncio
async def test_upload_ignores_legacy_origin_if_present(db_session: AsyncSession, tmp_path: Path) -> None:
    storage = PackStorageService(root=tmp_path)
    with_origin = _MANIFEST + "origin: uploaded\n"
    pack = await upload_pack(
        db_session,
        storage=storage,
        username="alice",
        origin_filename="x.tar.gz",
        data=_build_tarball(manifest=with_origin),
    )
    assert pack.id == "vendor-foo"


@pytest.mark.asyncio
async def test_re_upload_same_bytes_is_idempotent(db_session: AsyncSession, tmp_path: Path) -> None:
    storage = PackStorageService(root=tmp_path)
    data = _build_tarball()
    a = await upload_pack(db_session, storage=storage, username="alice", origin_filename="x.tar.gz", data=data)
    await db_session.flush()
    b = await upload_pack(db_session, storage=storage, username="alice", origin_filename="x.tar.gz", data=data)
    assert a.id == b.id
    # only one release row
    assert len(b.releases) == 1


@pytest.mark.asyncio
async def test_re_upload_with_changed_bytes_at_same_release_raises_409(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    storage = PackStorageService(root=tmp_path)
    await upload_pack(db_session, storage=storage, username="alice", origin_filename="x.tar.gz", data=_build_tarball())
    await db_session.flush()
    altered = _build_tarball(extra={"NOTES.txt": b"changed"})
    with pytest.raises(PackUploadConflictError):
        await upload_pack(db_session, storage=storage, username="alice", origin_filename="x.tar.gz", data=altered)
