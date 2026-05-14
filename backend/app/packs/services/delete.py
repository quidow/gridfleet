from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from app.models.device import Device
from app.packs.models import (
    DriverPack,
    DriverPackRelease,
    HostPackDoctorResult,
    HostPackFeatureStatus,
    HostPackInstallation,
)
from app.packs.services.lifecycle import count_active_work_for_pack

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def delete_pack(session: AsyncSession, pack_id: str) -> None:
    pack = (
        await session.execute(
            select(DriverPack)
            .where(DriverPack.id == pack_id)
            .options(
                selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms),
                selectinload(DriverPack.releases).selectinload(DriverPackRelease.features),
            )
        )
    ).scalar_one_or_none()
    if pack is None:
        raise LookupError(f"Pack {pack_id!r} not found")

    device_count = (
        await session.execute(select(func.count()).select_from(Device).where(Device.pack_id == pack_id))
    ).scalar_one()
    if device_count:
        noun = "device" if device_count == 1 else "devices"
        raise RuntimeError(f"Cannot delete pack {pack_id!r}; {device_count} {noun} still use it")

    active_work = await count_active_work_for_pack(session, pack_id)
    if active_work["active_runs"] or active_work["live_sessions"]:
        raise RuntimeError(
            f"Cannot delete pack {pack_id!r}; {active_work['active_runs']} active run(s) and "
            f"{active_work['live_sessions']} live session(s) still reference it"
        )

    artifact_paths = [Path(release.artifact_path) for release in pack.releases if release.artifact_path]

    await session.execute(delete(HostPackFeatureStatus).where(HostPackFeatureStatus.pack_id == pack_id))
    await session.execute(delete(HostPackDoctorResult).where(HostPackDoctorResult.pack_id == pack_id))
    await session.execute(delete(HostPackInstallation).where(HostPackInstallation.pack_id == pack_id))
    await session.delete(pack)
    await session.flush()

    for artifact_path in artifact_paths:
        artifact_path.unlink(missing_ok=True)
