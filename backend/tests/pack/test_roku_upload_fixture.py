from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.main import app
from app.models.driver_pack import DriverPackRelease
from app.routers.driver_pack_uploads import get_pack_storage
from app.services.pack_storage_service import PackStorageService

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "manifests"


async def test_roku_manifest_can_be_built_as_upload_tarball(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    # Prepare a pack directory from the test fixture manifest
    pack_dir = tmp_path / "appium-roku-dlenroc"
    pack_dir.mkdir()
    shutil.copy(_FIXTURES_DIR / "appium-roku-dlenroc.yaml", pack_dir / "manifest.yaml")

    out = tmp_path / "roku-upload.tar.gz"
    subprocess.run(
        [
            "python",
            "../scripts/build_driver_pack_tarball.py",
            "--pack-dir",
            str(pack_dir),
            "--out",
            str(out),
            "--id",
            "uploaded/appium-roku-dlenroc-fixture",
            "--release",
            "2026.04.0-fixture",
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=True,
    )
    assert out.exists()

    app.dependency_overrides[get_pack_storage] = lambda: PackStorageService(root=tmp_path / "storage")
    try:
        with out.open("rb") as handle:
            resp = await client.post(
                "/api/driver-packs/uploads",
                files={"tarball": ("roku-upload.tar.gz", handle, "application/gzip")},
            )
    finally:
        app.dependency_overrides.pop(get_pack_storage, None)

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "uploaded/appium-roku-dlenroc-fixture"
    assert body["current_release"] == "2026.04.0-fixture"

    release = (
        await db_session.execute(
            select(DriverPackRelease).where(
                DriverPackRelease.pack_id == "uploaded/appium-roku-dlenroc-fixture",
                DriverPackRelease.release == "2026.04.0-fixture",
            )
        )
    ).scalar_one()
    assert release.artifact_sha256
    assert release.artifact_path is not None
    assert Path(release.artifact_path).read_bytes() == out.read_bytes()
