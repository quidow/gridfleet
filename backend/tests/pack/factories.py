from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.driver_pack import (
    DriverPack,
    DriverPackFeature,
    DriverPackPlatform,
    DriverPackRelease,
    PackState,
)
from app.pack.manifest import Manifest, load_manifest_yaml

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "manifests"


@cache
def _load_test_manifests() -> tuple[Manifest, ...]:
    return tuple(
        load_manifest_yaml(manifest_path.read_text()) for manifest_path in sorted(_FIXTURES_DIR.glob("*.yaml"))
    )


async def seed_test_packs(session: AsyncSession, *, state: PackState | str = PackState.enabled) -> None:
    for manifest in _load_test_manifests():
        await seed_manifest_pack(session, manifest, state=state)


async def seed_manifest_pack(
    session: AsyncSession,
    manifest: Manifest,
    *,
    state: PackState | str = PackState.enabled,
) -> DriverPack:
    existing_pack = await session.get(DriverPack, manifest.id)
    if existing_pack is None:
        pack = DriverPack(
            id=manifest.id,
            origin="uploaded",
            display_name=manifest.display_name,
            maintainer=manifest.maintainer,
            license=manifest.license,
            state=state,
            runtime_policy={"strategy": "recommended"},
        )
        session.add(pack)
        await session.flush()
    else:
        pack = existing_pack

    existing_release = (
        await session.execute(
            select(DriverPackRelease).where(
                DriverPackRelease.pack_id == manifest.id,
                DriverPackRelease.release == manifest.release,
            )
        )
    ).scalar_one_or_none()
    if existing_release is not None:
        return pack

    manifest_json = manifest.model_dump(mode="json")
    release = DriverPackRelease(
        pack_id=manifest.id,
        release=manifest.release,
        manifest_json=manifest_json,
        derived_from_pack_id=manifest.derived_from.pack_id if manifest.derived_from else None,
        derived_from_release=manifest.derived_from.release if manifest.derived_from else None,
        template_id=manifest.template_id,
    )
    session.add(release)
    await session.flush()

    for platform in manifest.platforms:
        session.add(
            DriverPackPlatform(
                pack_release_id=release.id,
                manifest_platform_id=platform.id,
                display_name=platform.display_name,
                automation_name=platform.automation_name,
                appium_platform_name=platform.appium_platform_name,
                device_types=list(platform.device_types),
                connection_types=list(platform.connection_types),
                grid_slots=list(platform.grid_slots),
                data=platform.model_dump(mode="json"),
            )
        )

    for feature_id, feature in manifest.features.items():
        session.add(
            DriverPackFeature(
                pack_release_id=release.id,
                manifest_feature_id=feature_id,
                data=feature.model_dump(mode="json"),
            )
        )
    await session.flush()
    return pack
