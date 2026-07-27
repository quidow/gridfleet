"""Phase 9: ``GET /driver-packs/catalog`` is a pure read on a fixed query budget.

Two properties, both invisible to the API-shape tests:

* the read issues no writes and ends no transaction — the drain completion that
  used to run inside it belongs to the inline release hook and the janitor
  backstop; and
* its statement count does not grow with the fleet. The runtime summary was
  already batched into an ``IN`` pair; the per-draining-pack active-work count
  was not, so a catalog of 50 draining packs issued 50 extra query pairs.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.packs.models import (
    DriverPack,
    DriverPackPlatform,
    DriverPackRelease,
    HostPackInstallation,
    PackState,
)
from app.packs.services.lifecycle import PackLifecycleService
from app.packs.services.service import PackCatalogService
from tests.concurrency.group_lock_helpers import capture_statements

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.db]

_WRITE_VERBS = ("insert ", "update ", "delete ")


def _catalog_service() -> PackCatalogService:
    return PackCatalogService(lifecycle=PackLifecycleService())


async def _seed_packs(db: AsyncSession, host_id: uuid.UUID, *, already: int = 0, target: int) -> None:
    """Grow the seeded fleet from ``already`` to ``target`` packs, then commit.

    Every third pack is ``draining`` so each fleet size exercises both the
    active-work summary and the enabled fast path; pack 0 is draining, so the
    smallest size (1) never skips the draining branch and the statement counts
    stay comparable across sizes.
    """
    for index in range(already, target):
        pack_id = f"budget/pack-{index:03d}"
        state = PackState.draining if index % 3 == 0 else PackState.enabled
        db.add(DriverPack(id=pack_id, display_name=f"Pack {index}", state=state, current_release="1.0.0"))
        release = DriverPackRelease(
            pack_id=pack_id,
            release="1.0.0",
            manifest_json={"platforms": []},
        )
        db.add(release)
        await db.flush()
        db.add(
            DriverPackPlatform(
                pack_release_id=release.id,
                manifest_platform_id="budget_platform",
                display_name="Budget",
                automation_name="Budget",
                appium_platform_name="Budget",
                device_types=["real_device"],
                connection_types=["network"],
                data={"identity": {"scheme": "ip", "scope": "global"}},
            )
        )
        db.add(
            HostPackInstallation(
                host_id=host_id,
                pack_id=pack_id,
                pack_release="1.0.0",
                status="installed",
                appium_server_version="2.19.0",
                driver_specs=[{"package": "budget-driver", "version": "1.2.3"}],
            )
        )
    await db.commit()


async def _measure_catalog(db_session_maker: async_sessionmaker[AsyncSession], pack_count: int) -> list[str]:
    """Statements issued by one catalog read, on a session of its own.

    A fresh session keeps the measurement free of the seeding transaction and
    lets ``capture_statements`` pin its listener the way it requires.
    """
    async with db_session_maker() as reader, capture_statements(reader) as statements:
        catalog = await _catalog_service().list_catalog(reader)
        assert len(catalog.packs) == pack_count
    return statements


def _reading(statements: list[str], table: str) -> list[str]:
    return [sql for sql in statements if re.search(rf"\b{table}\b", sql.lower())]


async def test_catalog_read_issues_no_writes(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    await _seed_packs(db_session, db_host.id, target=4)

    statements = await _measure_catalog(db_session_maker, 4)

    writes = [sql for sql in statements if sql.lower().lstrip().startswith(_WRITE_VERBS)]
    assert writes == [], f"the catalog read must not mutate anything, but issued: {writes}"


async def test_catalog_leaves_a_zero_work_draining_pack_alone(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """Drain completion is the release hook's job, not the catalog read's."""
    await _seed_packs(db_session, db_host.id, target=3)

    await _measure_catalog(db_session_maker, 3)

    async with db_session_maker() as peer:
        state = await peer.scalar(select(DriverPack.state).where(DriverPack.id == "budget/pack-000"))
    assert state == PackState.draining, f"the catalog read completed a drain it must not touch; state={state}"


async def test_catalog_statement_count_is_flat_across_fleet_sizes(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    counts: dict[int, int] = {}
    seeded = 0
    for size in (1, 10, 50):
        await _seed_packs(db_session, db_host.id, already=seeded, target=size)
        seeded = size
        counts[size] = len(await _measure_catalog(db_session_maker, size))

    assert counts[1] == counts[10] == counts[50], (
        "the catalog read must cost the same number of statements at every fleet size; "
        f"got {counts} (SQL issued once per pack is the usual cause)"
    )


async def test_catalog_summaries_are_set_based(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """One ``IN`` pair for the runtime summary, one query for active work."""
    await _seed_packs(db_session, db_host.id, target=12)

    statements = await _measure_catalog(db_session_maker, 12)

    installation_reads = _reading(statements, "host_pack_installations")
    assert len(installation_reads) == 1, (
        f"the runtime summary must load installations once, not per pack: {len(installation_reads)} statements"
    )
    # driver_pack_releases is read twice in total: the eager selectinload behind
    # the catalog rows, and the runtime summary's driver-drift lookup.
    release_reads = _reading(statements, "driver_pack_releases")
    assert len(release_reads) == 2, f"expected one eager + one summary release read, got {len(release_reads)}"

    active_work_reads = _reading(statements, "device_reservations")
    assert len(active_work_reads) == 1, (
        f"active work across all draining packs must be one grouped query, got {len(active_work_reads)}"
    )
    assert re.search(r"\bsessions\b", active_work_reads[0].lower()), (
        "the active-work summary must count runs and live sessions in the same statement"
    )
