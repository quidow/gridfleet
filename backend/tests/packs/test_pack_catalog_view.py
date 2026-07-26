from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.packs.models import PackState
from app.packs.services.catalog_view import PackView, load_pack_catalog
from tests.concurrency.group_lock_helpers import capture_statements

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]

PACK_ID = "appium-uiautomator2"
PLATFORM_ID = "android_mobile"


async def test_load_pack_catalog_costs_one_statement(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    # capture_statements refuses a session that is already in a transaction, and
    # the seeded packs are only flushed — commit so a second session sees them.
    await db_session.commit()

    async with db_session_maker() as catalog_db, capture_statements(catalog_db) as statements:
        catalog = await load_pack_catalog(catalog_db, [PACK_ID])

    assert set(catalog) == {PACK_ID}
    assert len([sql for sql in statements if "driver_pack" in sql]) == 1


async def test_load_pack_catalog_reads_nothing_for_an_empty_id_set(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await db_session.commit()

    async with db_session_maker() as catalog_db, capture_statements(catalog_db) as statements:
        catalog = await load_pack_catalog(catalog_db, [])

    assert catalog == {}
    assert statements == []


async def test_catalog_survives_the_session_that_loaded_it(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The whole point: no attribute on the projection may need a live session.

    A `DriverPack` read here would raise MissingGreenlet on `pack.releases`
    under AsyncSession once the loading session is gone.
    """
    await db_session.commit()
    async with db_session_maker() as catalog_db:
        catalog = await load_pack_catalog(catalog_db, [PACK_ID])

    pack = catalog[PACK_ID]
    assert isinstance(pack, PackView)
    assert pack.state == PackState.enabled
    assert pack.releases
    platforms = [platform for release in pack.releases for platform in release.platforms]
    platform = next(p for p in platforms if p.manifest_platform_id == PLATFORM_ID)
    assert platform.automation_name
    assert platform.appium_platform_name
    assert isinstance(platform.data, dict)


async def test_projection_is_frozen(db_session: AsyncSession) -> None:
    catalog = await load_pack_catalog(db_session, [PACK_ID])
    pack = catalog[PACK_ID]

    with pytest.raises(AttributeError):
        pack.current_release = "9.9.9"  # type: ignore[misc]
