from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SessionFactory
    from app.packs.services.lifecycle import PackLifecycleService

from app.core.observability import get_logger
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
from app.packs.services.artifact_ledger import forget_artifacts, orphan_artifacts
from app.packs.services.driver_version import has_driver_drift, installed_driver_version
from app.packs.services.release_ordering import selected_release

logger = get_logger(__name__)


class PackNotFound(LookupError):  # noqa: N818  # matches the sibling PackPlatformNotFound
    """The pack a command was asked to mutate does not exist.

    Named, rather than a bare ``LookupError``, because the pack commands build
    their ``PackOut`` response snapshot *inside* the transaction while their
    router translates exceptions *outside* it. ``build_pack_out`` indexes
    persisted manifest and platform data (``data["source"]``,
    ``data["identity"]["scheme"]``), so a malformed row raises ``KeyError`` —
    itself a ``LookupError`` — from within the same ``try``. A router catching
    the base class would answer ``404 not found`` for a data bug that deserves a
    500. Subclasses ``LookupError`` so callers that still catch the base class,
    including the two delete routes, keep working unchanged.
    """


class PackTransitionError(ValueError):
    """The requested pack state transition is not allowed.

    Named for the same reason as :class:`PackNotFound`: ``build_pack_out`` runs
    ``RuntimePolicy.model_validate`` on the persisted policy column, and
    pydantic's ``ValidationError`` is a ``ValueError``. A router catching the
    base class would answer ``400`` with a validation dump to a caller whose
    request was perfectly valid.
    """


def unlink_pack_artifact(path: str) -> bool:
    """Remove a pack artifact whose metadata deletion has already committed.

    Called once the caller's transaction has ended, so the failure has nowhere
    to roll back to: the deletion the caller asked for did happen, and failing
    the response would report a rollback that never occurred. Swallowing it is
    now correct rather than merely defensible -- the artifact ledger row the
    same transaction marked ``orphaned`` guarantees the janitor retries.

    Returns:
        ``True`` when the file is gone, including when it was already missing;
        ``False`` when the unlink raised, in which case the caller must leave the
        ledger row alone.
    """
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("pack_artifact_unlink_failed", artifact_path=path, error=str(exc))
        return False
    return True


async def purge_pack_artifacts(session_factory: SessionFactory, paths: Sequence[str]) -> None:
    """Unlink committed-deleted artifacts, then forget the ones that went.

    Everything here runs after the caller's transaction committed, so nothing
    here may fail the response -- not the unlink, and not the ledger cleanup
    that follows it. Either one raised out of a delete route would answer 500
    for a deletion that is already durable, sending the operator looking for a
    pack that is gone.

    Both failure modes converge on the same place rather than on an operator: a
    file whose unlink failed keeps its ``orphaned`` row, and a row this could not
    drop stays ``orphaned`` too. Both are exactly the reaper's input, which is
    why swallowing them costs nothing.

    The unlink deliberately runs before the ``begin()`` rather than inside it:
    no transaction may span filesystem deletion, and the structure is what keeps
    that true instead of a comment asking the next editor to remember it.
    """
    reaped = [path for path in paths if unlink_pack_artifact(path)]
    if not reaped:
        return
    try:
        async with session_factory.begin() as db:
            await forget_artifacts(db, paths=reaped)
    except Exception:
        logger.exception("pack_artifact_forget_failed", artifact_paths=reaped)


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
        """Read the whole catalog. No writes, no boundary, no per-pack SQL.

        Drain completion used to run from here, which made a GET both a writer
        and the end of its caller's transaction; the inline release hook and the
        janitor backstop own it now. What remains is two batched summary loads
        whose cost does not move with the size of the fleet.
        """
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

        runtime_summaries = await self._runtime_summaries_by_pack(db, [pack.id for pack in rows])
        active_work = await self._lifecycle.summarize_active_work(
            db, [pack.id for pack in rows if pack.state == PackState.draining]
        )
        return PackCatalog(
            packs=[
                build_pack_out(
                    pack,
                    runtime_summaries.get(pack.id, PackRuntimeSummaryOut()),
                    active_runs=active_work.get(pack.id, {}).get("active_runs", 0),
                    live_sessions=active_work.get(pack.id, {}).get("live_sessions", 0),
                )
                for pack in rows
            ]
        )

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

    async def set_runtime_policy(self, db: AsyncSession, pack_id: str, policy: RuntimePolicy) -> PackOut:
        """Transaction-local; the router owns the boundary.

        One eager load replaces the previous load / commit / reload — the reload
        only existed because the commit landed in the middle of the method.
        """
        pack = (
            await db.execute(
                select(DriverPack)
                .options(
                    selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms),
                )
                .where(DriverPack.id == pack_id)
            )
        ).scalar_one_or_none()
        if pack is None:
            raise PackNotFound(pack_id)
        pack.runtime_policy = policy.model_dump()
        await db.flush()
        return build_pack_out(pack)

    async def delete_pack(self, db: AsyncSession, pack_id: str) -> list[str]:
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

        # Returned rather than unlinked here: filesystem work must not run inside
        # the transaction that deletes the metadata. Plain strings, so nothing
        # tied to this session crosses the boundary.
        artifact_paths = [release.artifact_path for release in pack.releases if release.artifact_path]
        # Marked in the same transaction that drops the metadata, so a crash
        # between this commit and the router's unlink still leaves a record that
        # the file is garbage.
        await orphan_artifacts(db, paths=artifact_paths)

        await db.execute(delete(HostPackDoctorResult).where(HostPackDoctorResult.pack_id == pack_id))
        await db.execute(delete(HostPackInstallation).where(HostPackInstallation.pack_id == pack_id))
        await db.delete(pack)
        await db.flush()
        return artifact_paths

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
