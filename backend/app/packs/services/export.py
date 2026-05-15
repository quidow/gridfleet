"""Service for exporting a driver-pack release as a gzip tarball.

Export behaviour:
  - Packs whose ``artifact_path`` is set: the stored tarball is
    returned verbatim (no re-packing overhead, hash preserved).
  - Older rows without an artifact get a fresh tarball synthesised on-the-fly
    from ``release.manifest_json``.

In both cases the caller receives ``(tarball_bytes, sha256_hex)``.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import tarfile
from typing import TYPE_CHECKING

import yaml
from sqlalchemy import select

from app.packs.models import DriverPackRelease
from app.packs.services.storage import PackStorageError, PackStorageService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def export_pack(
    session: AsyncSession,
    storage: PackStorageService,
    pack_id: str,
    release: str,
) -> tuple[bytes, str]:
    """Return ``(tarball_bytes, sha256_hex)`` for the given pack release.

    When a stored artifact exists, the existing bytes are read from disk via
    :class:`PackStorageService`. Older rows without artifacts are exported as a
    fresh single-entry tarball.

    Raises:
        LookupError: when no release row matches ``(pack_id, release)``.
    """
    row = (
        await session.execute(
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
            data = await asyncio.to_thread(_read_artifact, storage, row.artifact_path)
        except PackStorageError as exc:
            raise LookupError(f"artifact for pack {pack_id!r} release {release!r} is not readable: {exc}") from exc
    else:
        # Synthesise a tarball containing only manifest.yaml.
        data = await asyncio.to_thread(_synthesise_tarball, row.manifest_json)

    sha256 = hashlib.sha256(data).hexdigest()
    return data, sha256


def _read_artifact(storage: PackStorageService, path: str) -> bytes:
    with storage.open(path) as handle:
        return handle.read()


def _synthesise_tarball(manifest_json: dict[str, object]) -> bytes:
    """Build a gzip tarball with a single ``manifest.yaml`` entry.

    Args:
        manifest_json: The manifest dict stored in ``DriverPackRelease.manifest_json``.

    Returns:
        Compressed tarball bytes.
    """
    manifest_text = yaml.safe_dump(manifest_json, sort_keys=False)
    manifest_bytes = manifest_text.encode("utf-8")

    buf = io.BytesIO()
    with tarfile.open(mode="w:gz", fileobj=buf) as tar:
        info = tarfile.TarInfo(name="manifest.yaml")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))

    return buf.getvalue()
