"""Tests for the driver-pack export service (``PackReleaseService.export``).

Covers the artifact-less (manifest-only) release path, where the tarball is
synthesised on the fly from ``release.manifest_json`` via
``app.packs.services.export._synthesise_tarball``.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from app.packs.services.release import PackReleaseService
from app.packs.services.storage import PackStorageService

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.packs.models import DriverPack

pytestmark = pytest.mark.asyncio


def _extract_manifest_from_tarball(data: bytes) -> dict[str, Any]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.name in ("manifest.yaml", "./manifest.yaml"):
                handle = tar.extractfile(member)
                assert handle is not None
                return yaml.safe_load(handle.read())
    raise AssertionError("manifest.yaml not found in tarball")


async def test_export_synthesises_tarball_when_artifact_missing(
    db_session: AsyncSession,
    tmp_path: Path,
    uiautomator2_pack: DriverPack,
) -> None:
    """A release without a stored artifact is exported as a manifest-only tarball."""
    release = uiautomator2_pack.releases[0]
    assert release.artifact_path is None  # precondition: exercises the synthesise branch

    service = PackReleaseService(storage=PackStorageService(root=tmp_path))
    data, sha = await service.export(db_session, uiautomator2_pack.id, release.release)

    assert sha == hashlib.sha256(data).hexdigest()
    manifest = _extract_manifest_from_tarball(data)
    assert manifest["id"] == uiautomator2_pack.id
    assert manifest["release"] == release.release
