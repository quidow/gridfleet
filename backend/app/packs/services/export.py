"""Service for exporting a driver-pack release as a gzip tarball.

Export behaviour:
  - Packs whose ``artifact_path`` is set: the stored tarball is
    returned verbatim (no re-packing overhead, hash preserved).
  - Older rows without an artifact get a fresh tarball synthesised on-the-fly
    from ``release.manifest_json``.

In both cases the caller receives ``(tarball_bytes, sha256_hex)``.
"""

from __future__ import annotations

import io
import tarfile
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from app.packs.services.storage import PackStorageService


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
