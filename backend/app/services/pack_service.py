from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import and_, select
from sqlalchemy.orm import selectinload

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from app.models.driver_pack import DriverPack, DriverPackFeature, DriverPackPlatform, DriverPackRelease, PackState
from app.models.host_pack_installation import HostPackInstallation
from app.models.host_runtime_installation import HostRuntimeInstallation
from app.schemas.driver_pack import (
    AppiumInstallableOut,
    DerivedFrom,
    FeatureActionOut,
    FeatureOut,
    ManifestDoctorCheckOut,
    ManifestWorkaroundOut,
    PackCatalog,
    PackOut,
    PackPlatforms,
    PackRuntimeSummaryOut,
    PlatformOut,
    RuntimePolicy,
)
from app.services.pack_lifecycle_service import count_active_work_for_pack, try_complete_drain
from app.services.pack_release_ordering import selected_release


@dataclass
class _RuntimeSummaryAccumulator:
    installed_hosts: int = 0
    blocked_hosts: int = 0
    server_versions: set[str] = field(default_factory=set)
    driver_versions: set[str] = field(default_factory=set)
    driver_drift_hosts: int = 0


def _derived_from(release: DriverPackRelease | None) -> DerivedFrom | None:
    if release is None:
        return None
    if release.derived_from_pack_id and release.derived_from_release:
        return DerivedFrom(pack_id=release.derived_from_pack_id, release=release.derived_from_release)
    return None


def build_pack_out(pack: DriverPack, runtime_summary: PackRuntimeSummaryOut | None = None) -> PackOut:
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
        workarounds=_workarounds_out(manifest.get("workarounds", [])),
        doctor=_doctor_out(manifest.get("doctor", [])),
        insecure_features=manifest.get("insecure_features", []),
        features=_features_out(latest) if latest else {},
        runtime_policy=RuntimePolicy.model_validate(pack.runtime_policy or {"strategy": "recommended"}),
        derived_from=_derived_from(latest),
        runtime_summary=runtime_summary or PackRuntimeSummaryOut(),
    )


def _feature_out(feature: DriverPackFeature) -> FeatureOut:
    data = feature.data
    actions = [FeatureActionOut(id=a["id"], label=a.get("label", a["id"])) for a in data.get("actions", [])]
    return FeatureOut(
        display_name=data.get("display_name", feature.manifest_feature_id),
        description_md=data.get("description_md", ""),
        actions=actions,
    )


def _features_out(release: DriverPackRelease) -> dict[str, FeatureOut]:
    return {f.manifest_feature_id: _feature_out(f) for f in release.features}


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


def _workarounds_out(items: object) -> list[ManifestWorkaroundOut]:
    if not isinstance(items, list):
        return []
    return [
        ManifestWorkaroundOut(
            id=str(item["id"]),
            applies_when=dict(item.get("applies_when") or {}),
            env={str(key): str(value) for key, value in (item.get("env") or {}).items()},
        )
        for item in items
        if isinstance(item, dict) and "id" in item
    ]


def _doctor_out(items: object) -> list[ManifestDoctorCheckOut]:
    if not isinstance(items, list):
        return []
    return [
        ManifestDoctorCheckOut(
            id=str(item["id"]),
            description=str(item.get("description") or ""),
            adapter_hook=str(item["adapter_hook"]) if item.get("adapter_hook") is not None else None,
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
        grid_slots=platform.grid_slots,
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


async def list_catalog(session: AsyncSession) -> PackCatalog:
    rows = (
        (
            await session.execute(
                select(DriverPack)
                .options(
                    selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms),
                    selectinload(DriverPack.releases).selectinload(DriverPackRelease.features),
                )
                .order_by(DriverPack.id)
            )
        )
        .scalars()
        .all()
    )

    for pack in rows:
        if pack.state == PackState.draining:
            await try_complete_drain(session, pack.id)
    await session.commit()

    runtime_summaries = await _runtime_summaries_by_pack(session, [pack.id for pack in rows])
    out: list[PackOut] = []
    for pack in rows:
        latest = selected_release(pack.releases, pack.current_release)
        manifest = latest.manifest_json if latest else {}
        drain_info: dict[str, int] = {"active_runs": 0, "live_sessions": 0}
        if pack.state == PackState.draining:
            drain_info = await count_active_work_for_pack(session, pack.id)
        out.append(
            PackOut(
                id=pack.id,
                display_name=pack.display_name,
                maintainer=pack.maintainer,
                license=pack.license,
                state=pack.state,
                current_release=latest.release if latest else None,
                platforms=[_platform_out(platform) for platform in latest.platforms] if latest else [],
                appium_server=_installable_out(manifest.get("appium_server")),
                appium_driver=_installable_out(manifest.get("appium_driver")),
                workarounds=_workarounds_out(manifest.get("workarounds", [])),
                doctor=_doctor_out(manifest.get("doctor", [])),
                insecure_features=manifest.get("insecure_features", []),
                features=_features_out(latest) if latest else {},
                runtime_policy=RuntimePolicy.model_validate(pack.runtime_policy or {"strategy": "recommended"}),
                active_runs=drain_info["active_runs"],
                live_sessions=drain_info["live_sessions"],
                derived_from=_derived_from(latest),
                runtime_summary=runtime_summaries.get(pack.id, PackRuntimeSummaryOut()),
            )
        )
    return PackCatalog(packs=out)


async def get_pack_detail(session: AsyncSession, pack_id: str) -> PackOut | None:
    row = (
        await session.execute(
            select(DriverPack)
            .options(
                selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms),
                selectinload(DriverPack.releases).selectinload(DriverPackRelease.features),
            )
            .where(DriverPack.id == pack_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    runtime_summaries = await _runtime_summaries_by_pack(session, [row.id])
    return build_pack_out(row, runtime_summaries.get(row.id))


async def _runtime_summaries_by_pack(session: AsyncSession, pack_ids: list[str]) -> dict[str, PackRuntimeSummaryOut]:
    if not pack_ids:
        return {}

    rows = (
        await session.execute(
            select(HostPackInstallation, HostRuntimeInstallation)
            .outerjoin(
                HostRuntimeInstallation,
                and_(
                    HostRuntimeInstallation.host_id == HostPackInstallation.host_id,
                    HostRuntimeInstallation.runtime_id == HostPackInstallation.runtime_id,
                ),
            )
            .where(HostPackInstallation.pack_id.in_(pack_ids))
        )
    ).all()

    counters: dict[str, _RuntimeSummaryAccumulator] = {}
    for pack_row, runtime in rows:
        data = counters.setdefault(pack_row.pack_id, _RuntimeSummaryAccumulator())
        if pack_row.status == "installed":
            data.installed_hosts += 1
        if pack_row.status == "blocked":
            data.blocked_hosts += 1
        if runtime is not None:
            if runtime.appium_server_version:
                data.server_versions.add(runtime.appium_server_version)
            driver_version = _runtime_driver_version(runtime)
            if driver_version:
                data.driver_versions.add(driver_version)
        desired = _desired_driver_version(pack_row)
        actual = _runtime_driver_version(runtime) if runtime is not None else None
        if desired is not None and actual is not None and desired != actual:
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


def _desired_driver_version(pack_row: HostPackInstallation) -> str | None:
    spec = pack_row.resolved_install_spec or {}
    version = spec.get("appium_driver_version")
    if version is not None:
        return str(version)
    appium_driver = spec.get("appium_driver")
    if isinstance(appium_driver, dict) and appium_driver:
        first_version = next(iter(appium_driver.values()))
        return str(first_version) if first_version is not None else None
    return None


def _runtime_driver_version(runtime: HostRuntimeInstallation) -> str | None:
    if runtime.driver_specs:
        version = runtime.driver_specs[0].get("version")
        return str(version) if version is not None else None
    return None


async def get_platforms(session: AsyncSession, pack_id: str) -> PackPlatforms | None:
    pack = await session.scalar(
        select(DriverPack)
        .where(DriverPack.id == pack_id)
        .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms))
    )
    if pack is None:
        return None
    release = selected_release(pack.releases, pack.current_release)
    if release is None:
        return None
    return PackPlatforms(
        pack_id=pack_id,
        release=release.release,
        platforms=[_platform_out(platform) for platform in release.platforms],
    )
