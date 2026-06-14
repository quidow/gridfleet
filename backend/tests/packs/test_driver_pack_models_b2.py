from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.packs.models import DriverPack, DriverPackRelease

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_release_has_artifact_path(db_session: AsyncSession) -> None:
    pack = DriverPack(id="vendor-foo", origin="uploaded", display_name="Vendor", state="enabled")
    db_session.add(pack)
    await db_session.flush()
    release = DriverPackRelease(
        pack_id="vendor-foo",
        release="0.1.0",
        manifest_json={"id": "vendor-foo"},
        artifact_path="/var/gridfleet/vendor-foo/0.1.0.tar.gz",
        artifact_sha256="a" * 64,
    )
    db_session.add(release)
    await db_session.flush()
    assert release.artifact_path == "/var/gridfleet/vendor-foo/0.1.0.tar.gz"
