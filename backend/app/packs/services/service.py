from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.packs.services.lifecycle import PackLifecycleService

from app.devices.models import Device
from app.packs.models import (
    DriverPack,
    DriverPackPlatform,
    DriverPackRelease,
    HostPackDoctorResult,
    HostPackInstallation,
    PackState,
)
from app.packs.schemas import (
    AppiumInstallableOut,
    ManifestAppiumEnvOut,
    PackCatalog,
    PackOut,
    PackRuntimeSummaryOut,
    PlatformOut,
    RuntimePolicy,
)
from app.packs.services.driver_version import has_driver_drift, installed_driver_version
from app.packs.services.release_ordering import selected_release


@dataclass
class _RuntimeSummaryAccumulator:
    installed_hosts: int = 0
    blocked_hosts: int = 0
    server_versions: set[str] = field(default_factory=set)
    driver_versions: set[str] = field(default_factory=set)
    driver_drift_hosts: int = 0


def build_pack_out(
    pack: DriverPack,
    runtime_summary: PackRuntimeSummaryOut | None = None,
    *,
    active_runs: int = 0,
    live_sessions: int = 0,
) -> PackOut:
    latest = selected_release(pack.releases, pack.current_release)
    manifest = latest.manifest_json if latest else {}
    return PackOut(
        id=pack.id,
        display_name=pack.display_name,
        maintainer=pack.maintainer,
        license=pack.license,
        state=pack.state,
        current_release=latest.release if latest else None,
        platforms=[_platform_out(p) for p in latest.platforms] if latest else [],
        appium_server=_installable_out(manifest.get("appium_server")),
        appium_driver=_installable_out(manifest.get("appium_driver")),
        appium_env=_appium_env_out(manifest.get("appium_env", [])),
        insecure_features=manifest.get("insecure_features", []),
        runtime_policy=RuntimePolicy.model_validate(pack.runtime_policy or {"strategy": "recommended"}),
        active_runs=active_runs,
        live_sessions=live_sessions,
        runtime_summary=runtime_summary or PackRuntimeSummaryOut(),
    )


def _installable_out(data: object) -> AppiumInstallableOut | None:
    if not isinstance(data, dict):
        return None
    return AppiumInstallableOut(
        source=str(data["source"]),
        package=str(data["package"]),
        version=str(data["version"]),
        recommended=str(data["recommended"]) if data.get("recommended") is not None else None,
        known_bad=[str(version) for version in data.get("known_bad", [])],
        github_repo=str(data["github_repo"]) if data.get("github_repo") is not None else None,
    )


def _appium_env_out(items: object) -> list[ManifestAppiumEnvOut]:
    if not isinstance(items, list):
        return []
    return [
        ManifestAppiumEnvOut(
            id=str(item["id"]),
            applies_when=dict(item.get("applies_when") or {}),
            env={str(key): str(value) for key, value in (item.get("env") or {}).items()},
        )
        for item in items
        if isinstance(item, dict) and "id" in item
    ]


def _platform_out(platform: DriverPackPlatform) -> PlatformOut:
    return PlatformOut(
        id=platform.manifest_platform_id,
        display_name=platform.display_name,
        automation_name=platform.automation_name,
        appium_platform_name=platform.appium_platform_name,
        device_types=platform.device_types,
        connection_types=platform.connection_types,
        identity_scheme=platform.data["identity"]["scheme"],
        identity_scope=platform.data["identity"]["scope"],
        lifecycle_actions=platform.data.get("lifecycle_actions", []),
        health_checks=platform.data.get("health_checks", []),
        device_fields_schema=platform.data.get("device_fields_schema", []),
        capabilities=platform.data.get("capabilities", {}),
        display_metadata=platform.data.get("display") or {},
        default_capabilities=platform.data.get("default_capabilities") or {},
        connection_behavior=platform.data.get("connection_behavior") or {},
        parallel_resources=platform.data.get("parallel_resources") or {},
        device_type_overrides=platform.data.get("device_type_overrides") or {},
    )


class PackCatalogService:
    def __init__(self, *, lifecycle: PackLifecycleService) -> None:
        self._lifecycle = lifecycle

    async def list_catalog(self, db: AsyncSession) -> PackCatalog:
        rows = (
            (
                await db.execute(
                    select(DriverPack)
                    .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms))
                    .order_by(DriverPack.id)
                )
            )
            .scalars()
            .all()
        )

        for pack in rows:
            if pack.state == PackState.draining:
                await self._lifecycle.try_complete_drain(db, pack.id)
        await db.commit()

        runtime_summaries = await self._runtime_summaries_by_pack(db, [pack.id for pack in rows])
        out: list[PackOut] = []
        for pack in rows:
            drain_info: dict[str, int] = {"active_runs": 0, "live_sessions": 0}
            if pack.state == PackState.draining:
                drain_info = await self._lifecycle.count_active_work_for_pack(db, pack.id)
            out.append(
                build_pack_out(
                    pack,
                    runtime_summaries.get(pack.id, PackRuntimeSummaryOut()),
                    active_runs=drain_info["active_runs"],
                    live_sessions=drain_info["live_sessions"],
                )
            )
        return PackCatalog(packs=out)

    async def get_pack_detail(self, db: AsyncSession, pack_id: str) -> PackOut | None:
        row = (
            await db.execute(
                select(DriverPack)
                .options(
                    selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms),
                )
                .where(DriverPack.id == pack_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        runtime_summaries = await self._runtime_summaries_by_pack(db, [row.id])
        return build_pack_out(row, runtime_summaries.get(row.id))

    async def set_runtime_policy(self, db: AsyncSession, pack_id: str, policy: RuntimePolicy) -> DriverPack:
        pack = await db.get(DriverPack, pack_id)
        if pack is None:
            raise LookupError(pack_id)
        pack.runtime_policy = policy.model_dump()
        await db.commit()
        reloaded = (
            await db.execute(
                select(DriverPack)
                .options(
                    selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms),
                )
                .where(DriverPack.id == pack_id)
            )
        ).scalar_one()
        return reloaded

    async def delete_pack(self, db: AsyncSession, pack_id: str) -> None:
        pack = (
            await db.execute(
                select(DriverPack)
                .where(DriverPack.id == pack_id)
                .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms))
            )
        ).scalar_one_or_none()
        if pack is None:
            raise LookupError(f"Pack {pack_id!r} not found")

        device_count = (
            await db.execute(select(func.count()).select_from(Device).where(Device.pack_id == pack_id))
        ).scalar_one()
        if device_count:
            noun = "device" if device_count == 1 else "devices"
            raise RuntimeError(f"Cannot delete pack {pack_id!r}; {device_count} {noun} still use it")

        active_work = await self._lifecycle.count_active_work_for_pack(db, pack_id)
        if active_work["active_runs"] or active_work["live_sessions"]:
            raise RuntimeError(
                f"Cannot delete pack {pack_id!r}; {active_work['active_runs']} active run(s) and "
                f"{active_work['live_sessions']} live session(s) still reference it"
            )

        artifact_paths = [Path(release.artifact_path) for release in pack.releases if release.artifact_path]

        await db.execute(delete(HostPackDoctorResult).where(HostPackDoctorResult.pack_id == pack_id))
        await db.execute(delete(HostPackInstallation).where(HostPackInstallation.pack_id == pack_id))
        await db.delete(pack)
        await db.flush()

        for artifact_path in artifact_paths:
            artifact_path.unlink(missing_ok=True)

    async def _runtime_summaries_by_pack(
        self, db: AsyncSession, pack_ids: list[str]
    ) -> dict[str, PackRuntimeSummaryOut]:
        if not pack_ids:
            return {}

        rows = (
            (await db.execute(select(HostPackInstallation).where(HostPackInstallation.pack_id.in_(pack_ids))))
            .scalars()
            .all()
        )

        release_rows = (
            (await db.execute(select(DriverPackRelease).where(DriverPackRelease.pack_id.in_(pack_ids)))).scalars().all()
        )
        release_map = {(r.pack_id, r.release): r for r in release_rows}

        counters: dict[str, _RuntimeSummaryAccumulator] = {}
        for pack_row in rows:
            data = counters.setdefault(pack_row.pack_id, _RuntimeSummaryAccumulator())
            if pack_row.status == "installed":
                data.installed_hosts += 1
            if pack_row.status == "blocked":
                data.blocked_hosts += 1
            if pack_row.appium_server_version:
                data.server_versions.add(pack_row.appium_server_version)
            driver_version = installed_driver_version(pack_row)
            if driver_version:
                data.driver_versions.add(driver_version)
            release = release_map.get((pack_row.pack_id, pack_row.pack_release))
            if has_driver_drift(pack_row, release):
                data.driver_drift_hosts += 1

        summaries: dict[str, PackRuntimeSummaryOut] = {}
        for pack_id, data in counters.items():
            summaries[pack_id] = PackRuntimeSummaryOut(
                installed_hosts=data.installed_hosts,
                blocked_hosts=data.blocked_hosts,
                actual_appium_server_versions=sorted(data.server_versions),
                actual_appium_driver_versions=sorted(data.driver_versions),
                driver_drift_hosts=data.driver_drift_hosts,
            )
        return summaries
