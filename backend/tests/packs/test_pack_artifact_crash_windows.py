"""The three crash windows the upload's three phases open, and how they converge.

Each test kills the flow after one phase, then runs the reaper and asserts the
pair -- file and row -- ends consistent, and that no pack ever resolves to a
half-written file.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, select, update

from app.core.timeutil import now_utc
from app.packs.models import DriverPack, DriverPackRelease, PackArtifact, PackArtifactState
from app.packs.services.artifact_reaper import PACK_ARTIFACT_PENDING_GRACE_SEC, run_pack_artifact_reaper_stage
from app.packs.services.ingest import (
    PackIngestConflictError,
    ParsedPack,
    _store_artifact,
    activate_pack_upload,
    parse_pack_tarball,
    reserve_pack_upload,
)
from app.packs.services.storage import PackStorageService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.db

_MANIFEST = """\
schema_version: 1
id: vendor-foo
release: 0.1.0
display_name: __DISPLAY_NAME__
appium_server: { source: npm, package: appium, version: ">=2.5,<3", recommended: 2.19.0 }
appium_driver: { source: npm, package: appium-vendor-foo-driver, version: ">=0,<1", recommended: 0.1.0 }
platforms:
  - id: vendor_p
    display_name: Vendor
    automation_name: VendorAutomation
    appium_platform_name: Vendor
    device_types: [real_device]
    connection_types: [network]
    capabilities: { stereotype: {}, session_required: [] }
    identity: { scheme: vendor_uid, scope: global }
"""


def _tarball(display_name: str = "Vendor Foo") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        body = _MANIFEST.replace("__DISPLAY_NAME__", display_name).encode()
        info = tarfile.TarInfo(name="manifest.yaml")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


async def _age_out(sf: async_sessionmaker[AsyncSession]) -> None:
    """Push every pending reservation past its grace window."""
    async with sf.begin() as db:
        await db.execute(
            update(PackArtifact).values(
                state_changed_at=now_utc() - timedelta(seconds=PACK_ARTIFACT_PENDING_GRACE_SEC + 1)
            )
        )


async def _reap(sf: async_sessionmaker[AsyncSession]) -> None:
    async with sf() as db:
        await run_pack_artifact_reaper_stage(db)


async def test_crash_after_reserve_leaves_a_reservation_the_reaper_clears(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    storage = PackStorageService(tmp_path)
    parsed = parse_pack_tarball(_tarball())
    async with db_session_maker.begin() as db:
        reservation = await reserve_pack_upload(db, storage=storage, parsed=parsed)
    # crash here: no bytes, no metadata.

    assert not Path(reservation.artifact_path).exists()
    await _age_out(db_session_maker)
    await _reap(db_session_maker)

    assert (await db_session.scalars(select(PackArtifact))).all() == []
    assert (await db_session.scalars(select(DriverPackRelease))).all() == []


async def test_crash_after_write_leaves_a_file_the_reaper_clears(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    storage = PackStorageService(tmp_path)
    data = _tarball()
    parsed = parse_pack_tarball(data)
    async with db_session_maker.begin() as db:
        reservation = await reserve_pack_upload(db, storage=storage, parsed=parsed)
    _store_artifact(storage, pack_id=parsed.pack_id, release=parsed.release, data=data)
    # crash here: a complete file, no metadata that names it.

    assert Path(reservation.artifact_path).is_file()
    assert (await db_session.scalars(select(DriverPackRelease))).all() == [], (
        "no pack may resolve to a file whose activation never committed"
    )

    await _age_out(db_session_maker)
    await _reap(db_session_maker)

    assert not Path(reservation.artifact_path).exists()
    assert (await db_session.scalars(select(PackArtifact))).all() == []


async def test_crash_after_activate_leaves_a_live_pair_the_reaper_spares(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    storage = PackStorageService(tmp_path)
    data = _tarball()
    parsed = parse_pack_tarball(data)
    async with db_session_maker.begin() as db:
        reservation = await reserve_pack_upload(db, storage=storage, parsed=parsed)
    record = _store_artifact(storage, pack_id=parsed.pack_id, release=parsed.release, data=data)
    async with db_session_maker.begin() as db:
        await activate_pack_upload(
            db,
            parsed=parsed,
            reservation=reservation,
            record=record,
            username="admin",
            origin_filename="vendor-foo-0.1.0.tar.gz",
        )

    await _age_out(db_session_maker)
    await _reap(db_session_maker)

    assert Path(reservation.artifact_path).is_file()
    rows = (await db_session.scalars(select(PackArtifact))).all()
    assert [row.state for row in rows] == [PackArtifactState.active]
    assert rows[0].sha256 == record.sha256
    assert rows[0].size_bytes == record.size
    release = (await db_session.scalars(select(DriverPackRelease))).one()
    assert release.artifact_path == reservation.artifact_path


async def test_concurrent_reservations_grant_exactly_one_pending_writer(
    db_session_maker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Two real transactions cannot both own one new pack/release path."""
    storage = PackStorageService(tmp_path)
    parsed = [parse_pack_tarball(_tarball("Upload A")), parse_pack_tarball(_tarball("Upload B"))]
    barrier = asyncio.Barrier(2)

    async def _reserve(candidate: ParsedPack) -> object:
        async with db_session_maker.begin() as db:
            await barrier.wait()
            return await reserve_pack_upload(db, storage=storage, parsed=candidate)

    results = await asyncio.wait_for(
        asyncio.gather(*(_reserve(candidate) for candidate in parsed), return_exceptions=True),
        timeout=5,
    )

    assert len([result for result in results if not isinstance(result, BaseException)]) == 1
    assert len([result for result in results if isinstance(result, PackIngestConflictError)]) == 1


async def test_activation_rechecks_a_concurrently_created_release_hash(
    db_session_maker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A stale phase-one read cannot overwrite a peer's immutable release."""
    storage = PackStorageService(tmp_path)
    data = _tarball("Upload A")
    parsed = parse_pack_tarball(data)
    async with db_session_maker.begin() as db:
        reservation = await reserve_pack_upload(db, storage=storage, parsed=parsed)
    record = _store_artifact(storage, pack_id=parsed.pack_id, release=parsed.release, data=data)

    peer = parse_pack_tarball(_tarball("Upload B"))
    async with db_session_maker.begin() as db:
        db.add(
            DriverPack(
                id=peer.pack_id,
                display_name=peer.manifest.display_name,
                current_release=peer.release,
            )
        )
        await db.flush()
        db.add(
            DriverPackRelease(
                pack_id=peer.pack_id,
                release=peer.release,
                manifest_json=peer.manifest_dict,
                artifact_path="/peer/upload.tar.gz",
                artifact_sha256=peer.payload_sha,
            )
        )

    with pytest.raises(PackIngestConflictError):
        async with db_session_maker.begin() as db:
            await activate_pack_upload(
                db,
                parsed=parsed,
                reservation=reservation,
                record=record,
                username="admin",
                origin_filename="vendor-foo-0.1.0.tar.gz",
            )

    async with db_session_maker() as db:
        release = (await db.scalars(select(DriverPackRelease))).one()
    assert release.artifact_sha256 == peer.payload_sha


async def test_activation_rejects_a_reaped_reservation(
    db_session_maker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Metadata and its event cannot commit after the ledger claim disappeared."""
    storage = PackStorageService(tmp_path)
    data = _tarball()
    parsed = parse_pack_tarball(data)
    async with db_session_maker.begin() as db:
        reservation = await reserve_pack_upload(db, storage=storage, parsed=parsed)
    record = _store_artifact(storage, pack_id=parsed.pack_id, release=parsed.release, data=data)
    async with db_session_maker.begin() as db:
        await db.execute(delete(PackArtifact))

    with pytest.raises(PackIngestConflictError):
        async with db_session_maker.begin() as db:
            await activate_pack_upload(
                db,
                parsed=parsed,
                reservation=reservation,
                record=record,
                username="admin",
                origin_filename="vendor-foo-0.1.0.tar.gz",
            )

    async with db_session_maker() as db:
        assert (await db.scalars(select(DriverPackRelease))).all() == []


async def test_idempotent_activation_does_not_recreate_a_deleted_release(
    db_session_maker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A stale no-write phase must not mint a release with null artifact fields."""
    storage = PackStorageService(tmp_path)
    data = _tarball()
    parsed = parse_pack_tarball(data)
    async with db_session_maker.begin() as db:
        first = await reserve_pack_upload(db, storage=storage, parsed=parsed)
    record = _store_artifact(storage, pack_id=parsed.pack_id, release=parsed.release, data=data)
    async with db_session_maker.begin() as db:
        await activate_pack_upload(
            db,
            parsed=parsed,
            reservation=first,
            record=record,
            username="admin",
            origin_filename="vendor-foo-0.1.0.tar.gz",
        )
    assert first.needs_write

    async with db_session_maker.begin() as db:
        stale = await reserve_pack_upload(db, storage=storage, parsed=parsed)
    assert not stale.needs_write
    async with db_session_maker.begin() as db:
        await db.execute(delete(DriverPack).where(DriverPack.id == parsed.pack_id))

    with pytest.raises(PackIngestConflictError):
        async with db_session_maker.begin() as db:
            await activate_pack_upload(
                db,
                parsed=parsed,
                reservation=stale,
                record=None,
                username="admin",
                origin_filename="vendor-foo-0.1.0.tar.gz",
            )

    async with db_session_maker() as db:
        assert (await db.scalars(select(DriverPackRelease))).all() == []
