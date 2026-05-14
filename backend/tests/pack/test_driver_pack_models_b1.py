from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.packs.models import DriverPack, DriverPackRelease

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_driver_pack_release_has_derived_from_columns(db_session: AsyncSession) -> None:
    pack = DriverPack(
        id="local/foo",
        origin="uploaded",
        display_name="Foo",
        state="draft",
    )
    db_session.add(pack)
    await db_session.flush()
    release = DriverPackRelease(
        pack_id="local/foo",
        release="0.1.0",
        manifest_json={"id": "local/foo"},
        derived_from_pack_id="appium-uiautomator2",
        derived_from_release="2026.04.0",
        template_id=None,
    )
    db_session.add(release)
    await db_session.flush()
    assert release.derived_from_pack_id == "appium-uiautomator2"
    assert release.derived_from_release == "2026.04.0"
