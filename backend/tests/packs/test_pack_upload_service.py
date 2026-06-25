from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from app.packs.models import PackState
from app.packs.services.ingest import (
    MAX_PACK_MANIFEST_BYTES,
    MAX_PACK_TARBALL_BYTES,
    MAX_PACK_TARBALL_MEMBERS,
)
from app.packs.services.ingest import (
    PackIngestConflictError as PackUploadConflictError,
)
from app.packs.services.ingest import (
    PackIngestValidationError as PackUploadValidationError,
)
from app.packs.services.ingest import (
    ingest_pack_tarball as upload_pack,
)
from app.packs.services.storage import PackStorageService

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
async def test_upload_rejects_legacy_origin_if_present(db_session: AsyncSession, tmp_path: Path) -> None:
    storage = PackStorageService(root=tmp_path)
    with_origin = _MANIFEST + "origin: uploaded\n"
    with pytest.raises(PackUploadValidationError, match="origin"):
        await upload_pack(
            db_session,
            storage=storage,
            username="alice",
            origin_filename="x.tar.gz",
            data=_build_tarball(manifest=with_origin),
        )


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


@pytest.mark.asyncio
async def test_upload_warns_when_session_discovery_missing(
    db_session: AsyncSession, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """F3: a pack whose insecure_features lacks ':session_discovery' ingests but logs a
    non-fatal warning (orphan-session reaping is disabled for it)."""
    storage = PackStorageService(root=tmp_path)
    with caplog.at_level("WARNING", logger="app.packs.services.ingest"):
        await upload_pack(
            db_session, storage=storage, username="alice", origin_filename="x.tar.gz", data=_build_tarball()
        )
    assert any("pack_ingest_missing_session_discovery" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_upload_canonicalizes_session_discovery_into_stored_manifest(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Wave-5 #29: the invariant "every grid pack supports session enumeration" was
    split across a soft warn at ingest and a hard fix-up at dispatch (start_shim),
    so the stored manifest could permanently disagree with what runs. Ingest now
    canonicalizes the wildcard feature into the stored manifest_json; the dispatch
    injection stays as the compat layer for packs ingested before this."""
    from sqlalchemy import select

    from app.packs.models import DriverPackRelease

    storage = PackStorageService(root=tmp_path)
    await upload_pack(db_session, storage=storage, username="alice", origin_filename="x.tar.gz", data=_build_tarball())
    release = (
        (await db_session.execute(select(DriverPackRelease).where(DriverPackRelease.pack_id == "vendor-foo")))
        .scalars()
        .one()
    )
    assert "*:session_discovery" in (release.manifest_json.get("insecure_features") or [])


@pytest.mark.asyncio
async def test_upload_preserves_existing_session_discovery_scope(db_session: AsyncSession, tmp_path: Path) -> None:
    """A pack already requesting the feature (any scope) is stored verbatim — no
    duplicate wildcard appended."""
    from sqlalchemy import select

    from app.packs.models import DriverPackRelease

    storage = PackStorageService(root=tmp_path)
    manifest = _MANIFEST + 'insecure_features:\n  - "uiautomator2:session_discovery"\n'
    await upload_pack(
        db_session,
        storage=storage,
        username="alice",
        origin_filename="x.tar.gz",
        data=_build_tarball(manifest=manifest),
    )
    release = (
        (await db_session.execute(select(DriverPackRelease).where(DriverPackRelease.pack_id == "vendor-foo")))
        .scalars()
        .one()
    )
    assert release.manifest_json.get("insecure_features") == ["uiautomator2:session_discovery"]


@pytest.mark.asyncio
async def test_upload_no_warning_when_session_discovery_present(
    db_session: AsyncSession, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """F3: a pack that requests session_discovery ingests with no missing-feature warning."""
    storage = PackStorageService(root=tmp_path)
    manifest = _MANIFEST + 'insecure_features:\n  - "*:session_discovery"\n'
    with caplog.at_level("WARNING", logger="app.packs.services.ingest"):
        await upload_pack(
            db_session,
            storage=storage,
            username="alice",
            origin_filename="x.tar.gz",
            data=_build_tarball(manifest=manifest),
        )
    assert not any("pack_ingest_missing_session_discovery" in r.message for r in caplog.records)
