"""Value-shaped driver-pack catalog.

``DriverPack`` and its children are ORM rows: they belong to the session that
loaded them, and after that session commits, rolls back, or closes, touching
``pack.releases`` under ``AsyncSession`` raises ``MissingGreenlet``. Every
readiness verdict in the control plane reads only a handful of scalars off
them, so this module projects the catalog into frozen dataclasses that own
those scalars and outlive any session.

That is what lets a caller read the catalog ONCE per host and reuse it across
the short per-device transactions that settle each device — see
``app.appium_nodes.services.reconciler.converge_pushed_host``. The three
``db.expunge(pack)`` calls this replaced were all patching the same hazard.

This is the only place a ``DriverPack`` row becomes a value. Pack code that
mutates packs, serializes them, or resolves one platform still works on the
ORM rows directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import joinedload, load_only

from app.packs.models import DriverPack, DriverPackPlatform, DriverPackRelease

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.packs.models import PackState


@dataclass(frozen=True, slots=True)
class PackPlatformView:
    """One platform of one release, detached from its row.

    ``data`` is the manifest JSONB dict handed over by reference, not copied:
    it is already a plain deserialized dict by the time SQLAlchemy returns it,
    nothing in the control plane mutates it, and deep-copying every manifest on
    every catalog read is the expensive half of the read.
    """

    manifest_platform_id: str
    automation_name: str
    appium_platform_name: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PackReleaseView:
    release: str
    platforms: tuple[PackPlatformView, ...]


@dataclass(frozen=True, slots=True)
class PackView:
    # Redundant with the dict key in load_pack_catalog's return value, but a
    # PackView is also passed around bare with no key in hand (e.g.
    # assess_device_with_pack(device, pack)), so carrying its own id keeps it
    # self-describing outside that dict context.
    id: str
    state: PackState
    current_release: str | None
    releases: tuple[PackReleaseView, ...]


def project_pack(pack: DriverPack) -> PackView:
    """Project one eager-loaded ``DriverPack`` into an owned value.

    *pack* must carry ``releases`` and their ``platforms`` already loaded — this
    walks them and will trigger a lazy load (i.e. ``MissingGreenlet`` under
    ``AsyncSession``) if they are not.
    """
    return PackView(
        id=pack.id,
        state=pack.state,
        current_release=pack.current_release,
        releases=tuple(
            PackReleaseView(
                release=release.release,
                platforms=tuple(
                    PackPlatformView(
                        manifest_platform_id=platform.manifest_platform_id,
                        automation_name=platform.automation_name,
                        appium_platform_name=platform.appium_platform_name,
                        data=platform.data,
                    )
                    for platform in release.platforms
                ),
            )
            for release in pack.releases
        ),
    )


async def load_pack_catalog(session: AsyncSession, pack_ids: Iterable[str]) -> dict[str, PackView]:
    """One read: the named packs with their releases and platforms, as values.

    A single joined statement, with both axes declared.

    Rows: the two ``joinedload``s are a *chain* (pack -> releases -> platforms),
    not two sibling collections, so they do not form a cartesian product: the
    statement returns exactly one row per leaf platform, which is the minimum
    any strategy must transfer. ``.unique()`` deduplicates the repeated parent
    columns, not multiplied rows. Measured on a synthetic catalog (12 packs x 6
    retained releases x 8 platforms): 576 rows in 1 statement, against 660 rows
    in 3 statements for a ``selectinload(...).selectinload(...)`` equivalent —
    worse on both axes. Row volume is linear and trivial; do not "fix" this to
    ``selectinload``.

    Columns: ``load_only`` names exactly the columns ``project_pack`` reads.
    That matters more here than on an unjoined read, because a join repeats the
    parent's columns on every child row — an undeclared read fetched
    ``DriverPackRelease.manifest_json``, a large JSONB column nothing in the
    control plane touches, once per *platform* rather than once per release.
    Primary keys load regardless, which is why ``DriverPack.id`` is not named
    and still keys the returned dict. Anything added to ``PackView`` must be
    added here too, or ``project_pack`` raises on the deferred attribute.

    An empty or all-falsy *pack_ids* costs no statement at all, which is what
    keeps a host with no devices free.
    """
    ids = sorted({pack_id for pack_id in pack_ids if pack_id})
    if not ids:
        return {}
    packs = (
        (
            await session.scalars(
                select(DriverPack)
                .where(DriverPack.id.in_(ids))
                .options(
                    load_only(DriverPack.state, DriverPack.current_release),
                    joinedload(DriverPack.releases).load_only(DriverPackRelease.release),
                    joinedload(DriverPack.releases)
                    .joinedload(DriverPackRelease.platforms)
                    .load_only(
                        DriverPackPlatform.manifest_platform_id,
                        DriverPackPlatform.automation_name,
                        DriverPackPlatform.appium_platform_name,
                        DriverPackPlatform.data,
                    ),
                )
            )
        )
        .unique()
        .all()
    )
    return {pack.id: project_pack(pack) for pack in packs}
