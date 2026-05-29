from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.packs.models import DriverPackFeature
from app.packs.services.feature_dispatch import FeatureService
from app.packs.services.status import PackStatusService
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

_status_svc = PackStatusService(
    publisher=event_bus, feature=FeatureService(publisher=event_bus, circuit_breaker=Mock())
)

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


@pytest.fixture
def pack_storage_root(tmp_path: Path) -> Path:
    """Route pack storage to a per-test writable directory."""
    return tmp_path / "storage"


async def test_uploaded_sidecar_pack_populates_feature_and_desired_state(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host,  # noqa: ANN001
) -> None:
    manifest_text = (Path(__file__).parent / "fixtures" / "sidecar_upload_pack" / "manifest.yaml").read_text()
    response = await client.post(
        "/api/driver-packs/uploads",
        files={"tarball": ("sidecar.tar.gz", _tarball_bytes(manifest_text), "application/gzip")},
    )
    assert response.status_code == 201

    feature = (
        await db_session.execute(
            select(DriverPackFeature).where(DriverPackFeature.manifest_feature_id == "test_sidecar")
        )
    ).scalar_one()
    assert feature.manifest_feature_id == "test_sidecar"

    desired = await _status_svc.compute_desired(db_session, db_host.id)
    desired_pack = next(pack for pack in desired["packs"] if pack["id"] == "uploaded/sidecar-fixture")
    assert desired_pack["features"]["test_sidecar"]["sidecar"]["adapter_hook"] == "sidecar_lifecycle"
