from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.host import Host
from app.packs.models import DriverPack, PackState
from app.packs.services.host_compatibility import manifest_supports_host_os
from app.packs.services.release_ordering import selected_release
from app.plugins.models import AppiumPlugin

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


async def compute_desired(session: AsyncSession, host_id: uuid.UUID) -> dict[str, Any]:
    host = await session.get(Host, host_id)
    rows = (
        (
            await session.execute(
                select(DriverPack)
                .options(selectinload(DriverPack.releases))
                .where(DriverPack.state == PackState.enabled)
                .order_by(DriverPack.id)
            )
        )
        .scalars()
        .all()
    )

    plugin_rows = (
        (await session.execute(select(AppiumPlugin).where(AppiumPlugin.enabled.is_(True)).order_by(AppiumPlugin.name)))
        .scalars()
        .all()
    )
    plugins = [{"name": p.name, "version": p.version, "source": p.source, "package": p.package} for p in plugin_rows]

    packs: list[dict[str, Any]] = []
    for pack in rows:
        latest = selected_release(pack.releases, pack.current_release)
        if latest is None:
            continue
        manifest = latest.manifest_json
        if host is not None and not manifest_supports_host_os(manifest, str(host.os_type)):
            continue
        packs.append(
            {
                "id": pack.id,
                "release": latest.release,
                "appium_server": manifest["appium_server"],
                "appium_driver": manifest["appium_driver"],
                "platforms": manifest["platforms"],
                "features": manifest.get("features", {}),
                "requires": manifest.get("requires", {}),
                "runtime_policy": pack.runtime_policy or {"strategy": "recommended"},
                "tarball_sha256": latest.artifact_sha256,
            }
        )
    return {"host_id": str(host_id), "packs": packs, "plugins": plugins}
