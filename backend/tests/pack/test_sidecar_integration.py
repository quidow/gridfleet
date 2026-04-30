from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.main import app
from app.models.driver_pack import DriverPackFeature
from app.routers.driver_pack_uploads import get_pack_storage
from app.services.pack_desired_state_service import compute_desired
from app.services.pack_storage_service import PackStorageService

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


def _tarball_bytes(manifest_text: str) -> bytes:
    data = manifest_text.encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("manifest.yaml")
        info.size = len(data)
        info.mtime = 0
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def test_uploaded_sidecar_pack_populates_feature_and_desired_state(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host,  # noqa: ANN001
    tmp_path: Path,
) -> None:
    manifest_text = (Path(__file__).parent / "fixtures" / "sidecar_upload_pack" / "manifest.yaml").read_text()
    app.dependency_overrides[get_pack_storage] = lambda: PackStorageService(root=tmp_path / "storage")
    try:
        response = await client.post(
            "/api/driver-packs/uploads",
            files={"tarball": ("sidecar.tar.gz", _tarball_bytes(manifest_text), "application/gzip")},
        )
    finally:
        app.dependency_overrides.pop(get_pack_storage, None)
    assert response.status_code == 201

    feature = (
        await db_session.execute(
            select(DriverPackFeature).where(DriverPackFeature.manifest_feature_id == "test_sidecar")
        )
    ).scalar_one()
    assert feature.manifest_feature_id == "test_sidecar"

    desired = await compute_desired(db_session, db_host.id)
    desired_pack = next(pack for pack in desired["packs"] if pack["id"] == "uploaded/sidecar-fixture")
    assert desired_pack["features"]["test_sidecar"]["sidecar"]["adapter_hook"] == "sidecar_lifecycle"
