from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.packs.models import DriverPack, DriverPackRelease, PackState
from app.packs.services.catalog_view import PackView, load_pack_catalog
from tests.concurrency.group_lock_helpers import capture_statements

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]

PACK_ID = "appium-uiautomator2"
PLATFORM_ID = "android_mobile"

# The select list load_pack_catalog is allowed to fetch: the eight scalars
# project_pack reads, plus the primary keys SQLAlchemy always loads (they key
# the returned dict and identify the joined child rows).
CATALOG_COLUMNS = frozenset(
    {
        "driver_packs.id",
        "driver_packs.state",
        "driver_packs.current_release",
        "driver_pack_releases.id",
        "driver_pack_releases.release",
        "driver_pack_platforms.id",
        "driver_pack_platforms.manifest_platform_id",
        "driver_pack_platforms.automation_name",
        "driver_pack_platforms.appium_platform_name",
        "driver_pack_platforms.data",
    }
)


def _selected_columns(sql: str) -> frozenset[str]:
    """The select list of *sql*, with SQLAlchemy's join aliases normalised away.

    ``driver_pack_platforms_1.data`` and ``driver_pack_releases_1.id AS id_2``
    name the same columns as ``driver_pack_platforms.data`` and
    ``driver_pack_releases.id``; the ``_1`` suffix and the ``AS`` label are
    alias bookkeeping, not part of what the statement fetches.
    """
    select_list = sql.split("SELECT", 1)[1].split("FROM", 1)[0]
    return frozenset(re.sub(r"_\d+\.", ".", item.strip().split(" AS ")[0]) for item in select_list.split(","))


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


async def test_load_pack_catalog_reads_nothing_for_an_all_falsy_id_set(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A non-empty collection of only falsy ids must also short-circuit.

    Distinct from the empty-list case above: this exercises the truthiness
    filter itself (an empty-string ``pack_id`` is the realistic production
    shape — a device row with no pack assigned), not just the `not ids`
    fast path that an empty list hits before the filter ever runs.
    """
    await db_session.commit()

    async with db_session_maker() as catalog_db, capture_statements(catalog_db) as statements:
        catalog = await load_pack_catalog(catalog_db, [""])

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


async def test_catalog_read_fetches_only_the_projected_columns(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The query declares the same columns ``PackView`` declares — no more, no less.

    A join duplicates parent columns across child rows, so an undeclared column
    is worse than a flat overfetch: ``DriverPackRelease.manifest_json`` would be
    transferred once per platform rather than once per release. The negative
    half of this assertion (nothing extra) is that guard. The positive half
    (nothing missing) is guarded twice: a column dropped from the ``load_only``
    set but still read by ``project_pack`` raises ``MissingGreenlet`` in
    ``test_catalog_survives_the_session_that_loaded_it`` below.
    """
    await db_session.commit()

    async with db_session_maker() as catalog_db, capture_statements(catalog_db) as statements:
        await load_pack_catalog(catalog_db, [PACK_ID])

    [sql] = [statement for statement in statements if "driver_pack" in statement]
    assert _selected_columns(sql) == CATALOG_COLUMNS


async def test_deferred_columns_still_load_for_a_later_full_read(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A catalog read leaves its ORM rows in the session's identity map with every
    undeclared column deferred. Any later full read of those same rows on that
    same session — ``load_platform_label_map``, ``platform_resolver`` — must
    populate them, not hand back a row that raises ``MissingGreenlet`` on
    ``manifest_json``.

    SQLAlchemy populates the *unloaded* attributes of an instance it already
    holds, which is what makes the ``load_only`` above safe for sessions that do
    both reads. This test is the pin on that behaviour; without it the safety of
    the whole change rests on an unstated framework detail.
    """
    await db_session.commit()

    async with db_session_maker() as shared_db:
        await load_pack_catalog(shared_db, [PACK_ID])
        pack = (
            await shared_db.scalars(
                select(DriverPack)
                .where(DriverPack.id == PACK_ID)
                .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms))
            )
        ).one()

        assert pack.display_name
        assert pack.runtime_policy
        for release in pack.releases:
            assert release.manifest_json
            for platform in release.platforms:
                assert platform.display_name
