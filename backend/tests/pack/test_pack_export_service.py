"""Tests for pack_export_service — Step 1 (TDD: failing tests first).

Service under test:
    app.services.pack_export_service.export_pack(session, storage, pack_id, release)
        -> tuple[bytes, str]

Cases:
  - test_export_uploaded_pack_returns_artifact_bytes
  - test_export_pack_synthesises_tarball_when_artifact_missing
  - test_export_unknown_release_raises_lookup_error
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from typing import TYPE_CHECKING

import pytest
import yaml

from app.models.driver_pack import DriverPack, DriverPackRelease, PackState
from app.services.pack_export_service import export_pack
from app.services.pack_storage_service import PackStorageService

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VENDOR_MANIFEST = """\
schema_version: 1
id: vendor-export
release: 0.2.0
display_name: Vendor Export
appium_server: { source: npm, package: appium, version: ">=2.5,<3", recommended: 2.19.0 }
appium_driver: { source: npm, package: appium-vendor-exp-driver, version: ">=0,<1", recommended: 0.1.0 }
platforms:
  - id: vendor_p
    display_name: Vendor
    automation_name: VendorAutomation
    appium_platform_name: Vendor
    device_types: [real_device]
    connection_types: [network]
    grid_slots: [native]
    capabilities: { stereotype: {}, session_required: [] }
    identity: { scheme: vendor_uid, scope: global }
"""


def _build_vendor_tarball() -> bytes:
    """Return a minimal tarball containing manifest.yaml with vendor metadata."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        body = _VENDOR_MANIFEST.encode()
        info = tarfile.TarInfo(name="manifest.yaml")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _extract_manifest_from_tarball(data: bytes) -> dict:
    """Extract and return parsed manifest.yaml from a tarball."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.name in ("manifest.yaml", "./manifest.yaml"):
                handle = tar.extractfile(member)
                assert handle is not None
                return yaml.safe_load(handle.read())
    raise AssertionError("manifest.yaml not found in tarball")


async def _add_uploaded_release(
    db_session: AsyncSession,
    tmp_path: Path,
    *,
    pack_id: str = "vendor-export",
    release: str = "0.2.0",
    tarball_data: bytes | None = None,
) -> tuple[DriverPack, DriverPackRelease, PackStorageService]:
    """Create an uploaded DriverPack + DriverPackRelease with a stored artifact."""
    if tarball_data is None:
        tarball_data = _build_vendor_tarball()

    storage = PackStorageService(root=tmp_path)
    record = storage.store(pack_id=pack_id, release=release, data=tarball_data)

    manifest_dict = yaml.safe_load(_VENDOR_MANIFEST)

    pack = DriverPack(
        id=pack_id,
        origin="uploaded",
        display_name="Vendor Export",
        maintainer="",
        license="",
        state=PackState.enabled,
        runtime_policy={"strategy": "recommended"},
    )
    db_session.add(pack)
    await db_session.flush()

    rel = DriverPackRelease(
        pack_id=pack_id,
        release=release,
        manifest_json=manifest_dict,
        artifact_path=record.path,
        artifact_sha256=record.sha256,
    )
    db_session.add(rel)
    await db_session.flush()

    return pack, rel, storage


# ---------------------------------------------------------------------------
# Test: uploaded pack returns existing artifact bytes
# ---------------------------------------------------------------------------


async def test_export_uploaded_pack_returns_artifact_bytes(db_session: AsyncSession, tmp_path: Path) -> None:
    """export_pack for an uploaded pack with artifact_path returns the stored bytes."""
    original_tarball = _build_vendor_tarball()
    pack, rel, storage = await _add_uploaded_release(db_session, tmp_path, tarball_data=original_tarball)

    result_bytes, sha = await export_pack(db_session, storage, pack.id, rel.release)

    assert result_bytes == original_tarball
    assert sha == hashlib.sha256(original_tarball).hexdigest()


# ---------------------------------------------------------------------------
# Test: pack synthesises a tarball from manifest_json when artifact is missing
# ---------------------------------------------------------------------------


async def test_export_pack_synthesises_tarball_when_artifact_missing(
    db_session: AsyncSession,
    tmp_path: Path,
    uiautomator2_pack,  # noqa: ANN001  (fixture from conftest)
) -> None:
    """Rows without artifacts are exported as a manifest tarball."""
    storage = PackStorageService(root=tmp_path)

    pack = uiautomator2_pack
    release = pack.releases[0]

    result_bytes, sha = await export_pack(db_session, storage, pack.id, release.release)

    assert sha == hashlib.sha256(result_bytes).hexdigest()

    manifest_dict = _extract_manifest_from_tarball(result_bytes)
    assert manifest_dict["id"] == pack.id
    assert manifest_dict["release"] == release.release
    assert "origin" not in manifest_dict


# ---------------------------------------------------------------------------
# Test: unknown release raises LookupError
# ---------------------------------------------------------------------------


async def test_export_unknown_release_raises_lookup_error(db_session: AsyncSession, tmp_path: Path) -> None:
    """export_pack raises LookupError when the pack+release combination is not found."""
    storage = PackStorageService(root=tmp_path)

    with pytest.raises(LookupError, match="not found"):
        await export_pack(db_session, storage, "nonexistent-pack", "9.9.9")
