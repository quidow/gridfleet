from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.packs.models import DriverPack, DriverPackRelease
from app.packs.services import release_ordering as pack_release_ordering

selected_release = pack_release_ordering.selected_release

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

PackPlatformKey = tuple[str, str]


async def load_platform_label_map(
    session: AsyncSession,
    pairs: Iterable[PackPlatformKey],
) -> dict[PackPlatformKey, str | None]:
    requested = {(pack_id, platform_id) for pack_id, platform_id in pairs if pack_id and platform_id}
    if not requested:
        return {}

    labels: dict[PackPlatformKey, str | None] = {pair: None for pair in requested}
    result = await session.execute(
        select(DriverPack)
        .where(DriverPack.id.in_(sorted({pack_id for pack_id, _ in requested})))
        .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms))
    )

    for pack in result.scalars().all():
        release = selected_release(list(pack.releases), pack.current_release)
        if release is None:
            continue
        for platform in release.platforms:
            key = (pack.id, platform.manifest_platform_id)
            if key in labels:
                labels[key] = platform.display_name

    return labels


async def load_platform_label(
    session: AsyncSession,
    *,
    pack_id: str,
    platform_id: str,
) -> str | None:
    labels = await load_platform_label_map(session, [(pack_id, platform_id)])
    return labels.get((pack_id, platform_id))
