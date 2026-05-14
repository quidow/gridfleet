from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

from app.models.host import Host
from app.models.host_plugin_runtime_status import HostPluginRuntimeStatus
from app.packs.models import (
    DriverPackRelease,
    HostPackDoctorResult,
    HostPackFeatureStatus,
    HostPackInstallation,
    HostRuntimeInstallation,
)
from app.packs.services.feature_status import record_feature_status
from app.packs.services.host_compatibility import manifest_supports_host_os

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def apply_status(session: AsyncSession, payload: dict[str, Any]) -> None:
    host_id = uuid.UUID(payload["host_id"])

    for rt in payload.get("runtimes", []):
        existing = (
            await session.execute(
                select(HostRuntimeInstallation).where(
                    HostRuntimeInstallation.host_id == host_id,
                    HostRuntimeInstallation.runtime_id == rt["runtime_id"],
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                HostRuntimeInstallation(
                    host_id=host_id,
                    runtime_id=rt["runtime_id"],
                    appium_server_package=rt["appium_server"]["package"],
                    appium_server_version=rt["appium_server"]["version"],
                    driver_specs=rt.get("appium_driver", []),
                    plugin_specs=[],
                    appium_home=rt.get("appium_home"),
                    status=rt.get("status", "pending"),
                    blocked_reason=rt.get("blocked_reason"),
                )
            )
        else:
            existing.appium_server_package = rt["appium_server"]["package"]
            existing.appium_server_version = rt["appium_server"]["version"]
            existing.driver_specs = rt.get("appium_driver", [])
            existing.plugin_specs = existing.plugin_specs or []
            existing.appium_home = rt.get("appium_home")
            existing.status = rt.get("status", existing.status)
            existing.blocked_reason = rt.get("blocked_reason")

        for plugin in rt.get("appium_plugins", []):
            await upsert_plugin_status(
                session,
                host_id=host_id,
                runtime_id=rt["runtime_id"],
                plugin_name=plugin["name"],
                version=plugin["version"],
                status=plugin.get("status", "unknown"),
                blocked_reason=plugin.get("blocked_reason"),
            )

    for pack in payload.get("packs", []):
        existing_pack = (
            await session.execute(
                select(HostPackInstallation).where(
                    HostPackInstallation.host_id == host_id,
                    HostPackInstallation.pack_id == pack["pack_id"],
                )
            )
        ).scalar_one_or_none()
        if existing_pack is None:
            session.add(
                HostPackInstallation(
                    host_id=host_id,
                    pack_id=pack["pack_id"],
                    pack_release=pack["pack_release"],
                    runtime_id=pack.get("runtime_id"),
                    status=pack.get("status", "pending"),
                    resolved_install_spec=pack.get("resolved_install_spec"),
                    installer_log_excerpt=pack.get("installer_log_excerpt"),
                    resolver_version=pack.get("resolver_version"),
                    blocked_reason=pack.get("blocked_reason"),
                    installed_at=datetime.now(UTC) if pack.get("status") == "installed" else None,
                )
            )
        else:
            existing_pack.pack_release = pack["pack_release"]
            existing_pack.runtime_id = pack.get("runtime_id")
            existing_pack.status = pack.get("status", existing_pack.status)
            existing_pack.resolved_install_spec = pack.get("resolved_install_spec")
            existing_pack.installer_log_excerpt = pack.get("installer_log_excerpt")
            existing_pack.resolver_version = pack.get("resolver_version")
            existing_pack.blocked_reason = pack.get("blocked_reason")
            if pack.get("status") == "installed":
                existing_pack.installed_at = datetime.now(UTC)

    doctor_scope_pack_ids = {p["pack_id"] for p in payload.get("packs", []) if p.get("status") == "installed"}
    if doctor_scope_pack_ids:
        await session.execute(
            delete(HostPackDoctorResult).where(
                HostPackDoctorResult.host_id == host_id,
                HostPackDoctorResult.pack_id.in_(doctor_scope_pack_ids),
            )
        )
    for d in payload.get("doctor", []):
        if d["pack_id"] not in doctor_scope_pack_ids:
            continue
        session.add(
            HostPackDoctorResult(
                host_id=host_id,
                pack_id=d["pack_id"],
                check_id=d["check_id"],
                ok=d["ok"],
                message=d.get("message", ""),
            )
        )

    for sidecar in payload.get("sidecars", []):
        await record_feature_status(
            session,
            host_id=host_id,
            pack_id=sidecar["pack_id"],
            feature_id=sidecar["feature_id"],
            ok=bool(sidecar["ok"]),
            detail=str(sidecar.get("detail") or sidecar.get("last_error") or sidecar.get("state") or ""),
        )


async def get_host_driver_pack_status(session: AsyncSession, host_id: uuid.UUID) -> dict[str, Any]:
    host = await session.get(Host, host_id)
    packs = (
        (
            await session.execute(
                select(HostPackInstallation)
                .where(HostPackInstallation.host_id == host_id)
                .order_by(HostPackInstallation.pack_id)
            )
        )
        .scalars()
        .all()
    )
    reported_pack_ids = {row.pack_id for row in packs}
    pack_release_map: dict[tuple[str, str], DriverPackRelease] = {}
    if packs:
        pack_ids = {row.pack_id for row in packs}
        releases = (
            (await session.execute(select(DriverPackRelease).where(DriverPackRelease.pack_id.in_(pack_ids))))
            .scalars()
            .all()
        )
        pack_release_map = {(release.pack_id, release.release): release for release in releases}
    if host is not None:
        packs = [row for row in packs if _pack_row_supports_host(row, host, pack_release_map)]
    compatible_pack_ids = {row.pack_id for row in packs}
    runtimes = (
        (
            await session.execute(
                select(HostRuntimeInstallation)
                .where(HostRuntimeInstallation.host_id == host_id)
                .order_by(HostRuntimeInstallation.runtime_id)
            )
        )
        .scalars()
        .all()
    )
    runtime_map = {runtime.runtime_id: runtime for runtime in runtimes}
    doctor = (
        (
            await session.execute(
                select(HostPackDoctorResult)
                .where(HostPackDoctorResult.host_id == host_id)
                .order_by(HostPackDoctorResult.pack_id, HostPackDoctorResult.check_id)
            )
        )
        .scalars()
        .all()
    )
    if host is not None:
        doctor = [row for row in doctor if row.pack_id not in reported_pack_ids or row.pack_id in compatible_pack_ids]
    plugin_rows = (
        (
            await session.execute(
                select(HostPluginRuntimeStatus)
                .where(HostPluginRuntimeStatus.host_id == host_id)
                .order_by(HostPluginRuntimeStatus.runtime_id, HostPluginRuntimeStatus.plugin_name)
            )
        )
        .scalars()
        .all()
    )
    feature_rows = (
        (
            await session.execute(
                select(HostPackFeatureStatus)
                .where(HostPackFeatureStatus.host_id == host_id)
                .order_by(HostPackFeatureStatus.pack_id, HostPackFeatureStatus.feature_id)
            )
        )
        .scalars()
        .all()
    )
    if host is not None:
        feature_rows = [
            row for row in feature_rows if row.pack_id not in reported_pack_ids or row.pack_id in compatible_pack_ids
        ]
    plugins_by_runtime: dict[str, list[dict[str, Any]]] = {}
    for row in plugin_rows:
        plugins_by_runtime.setdefault(row.runtime_id, []).append(
            {
                "name": row.plugin_name,
                "version": row.version,
                "status": row.status,
                "blocked_reason": row.blocked_reason,
            }
        )
    return {
        "host_id": host_id,
        "packs": [
            {
                "pack_id": row.pack_id,
                "pack_release": row.pack_release,
                "runtime_id": row.runtime_id,
                "status": row.status,
                "resolved_install_spec": row.resolved_install_spec,
                "installer_log_excerpt": row.installer_log_excerpt,
                "resolver_version": row.resolver_version,
                "blocked_reason": row.blocked_reason,
                "installed_at": row.installed_at,
                "desired_appium_driver_version": _desired_driver_version(row, pack_release_map),
                "installed_appium_driver_version": _installed_driver_version(row, runtime_map),
                "appium_driver_drift": _compute_drift(row, pack_release_map, runtime_map),
            }
            for row in packs
        ],
        "runtimes": [
            {
                "runtime_id": row.runtime_id,
                "appium_server_package": row.appium_server_package,
                "appium_server_version": row.appium_server_version,
                "driver_specs": row.driver_specs or [],
                "plugin_specs": row.plugin_specs or [],
                "appium_home": row.appium_home,
                "status": row.status,
                "blocked_reason": row.blocked_reason,
                "plugins": plugins_by_runtime.get(row.runtime_id, []),
            }
            for row in runtimes
        ],
        "doctor": [
            {
                "pack_id": row.pack_id,
                "check_id": row.check_id,
                "ok": row.ok,
                "message": row.message,
            }
            for row in doctor
        ],
        "features": [
            {
                "pack_id": row.pack_id,
                "feature_id": row.feature_id,
                "ok": row.ok,
                "detail": row.detail,
            }
            for row in feature_rows
        ],
    }


async def get_driver_pack_host_status(session: AsyncSession, pack_id: str) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(HostPackInstallation, Host)
            .join(Host, Host.id == HostPackInstallation.host_id)
            .where(HostPackInstallation.pack_id == pack_id)
            .order_by(Host.hostname)
        )
    ).all()
    releases = (
        (await session.execute(select(DriverPackRelease).where(DriverPackRelease.pack_id == pack_id))).scalars().all()
    )
    pack_release_map = {(release.pack_id, release.release): release for release in releases}

    compatible_rows = [row for row in rows if _pack_row_supports_host(row[0], row[1], pack_release_map)]
    pack_rows = [row[0] for row in compatible_rows]
    host_by_id = {row[1].id: row[1] for row in compatible_rows}

    runtime_ids = {row.runtime_id for row in pack_rows if row.runtime_id}
    host_ids = {row.host_id for row in pack_rows}
    runtime_map: dict[tuple[uuid.UUID, str], HostRuntimeInstallation] = {}
    if runtime_ids:
        runtimes = (
            (
                await session.execute(
                    select(HostRuntimeInstallation).where(
                        HostRuntimeInstallation.host_id.in_(host_ids),
                        HostRuntimeInstallation.runtime_id.in_(runtime_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        runtime_map = {(runtime.host_id, runtime.runtime_id): runtime for runtime in runtimes}

    doctor_rows = (
        (
            await session.execute(
                select(HostPackDoctorResult)
                .where(HostPackDoctorResult.pack_id == pack_id)
                .order_by(HostPackDoctorResult.check_id)
            )
        )
        .scalars()
        .all()
    )
    if host_by_id:
        doctor_rows = [row for row in doctor_rows if row.host_id in host_by_id]
    doctor_by_host: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for row in doctor_rows:
        doctor_by_host.setdefault(row.host_id, []).append(
            {"check_id": row.check_id, "ok": row.ok, "message": row.message}
        )

    hosts: list[dict[str, Any]] = []
    for pack_row in pack_rows:
        host = host_by_id[pack_row.host_id]
        runtime = runtime_map.get((pack_row.host_id, pack_row.runtime_id)) if pack_row.runtime_id is not None else None
        runtime_by_id = {runtime.runtime_id: runtime} if runtime is not None else {}
        hosts.append(
            {
                "host_id": str(pack_row.host_id),
                "hostname": host.hostname,
                "status": host.status,
                "pack_release": pack_row.pack_release,
                "runtime_id": pack_row.runtime_id,
                "pack_status": pack_row.status,
                "resolved_install_spec": pack_row.resolved_install_spec,
                "installer_log_excerpt": pack_row.installer_log_excerpt,
                "resolver_version": pack_row.resolver_version,
                "blocked_reason": pack_row.blocked_reason,
                "installed_at": pack_row.installed_at,
                "desired_appium_driver_version": _desired_driver_version(pack_row, pack_release_map),
                "installed_appium_driver_version": _installed_driver_version(pack_row, runtime_by_id),
                "appium_driver_drift": _compute_drift(pack_row, pack_release_map, runtime_by_id),
                "appium_home": runtime.appium_home if runtime is not None else None,
                "runtime_status": runtime.status if runtime is not None else None,
                "runtime_blocked_reason": runtime.blocked_reason if runtime is not None else None,
                "appium_server_version": runtime.appium_server_version if runtime is not None else None,
                "doctor": doctor_by_host.get(pack_row.host_id, []),
            }
        )

    return {"pack_id": pack_id, "hosts": hosts}


def _pack_row_supports_host(
    pack_row: HostPackInstallation,
    host: Host,
    pack_release_map: dict[tuple[str, str], DriverPackRelease],
) -> bool:
    release = pack_release_map.get((pack_row.pack_id, pack_row.pack_release))
    if release is None:
        release = next(
            (candidate for key, candidate in pack_release_map.items() if key[0] == pack_row.pack_id),
            None,
        )
    if release is None:
        return True
    return manifest_supports_host_os(release.manifest_json, str(host.os_type))


def _desired_driver_version(
    pack_row: HostPackInstallation,
    pack_release_map: dict[tuple[str, str], DriverPackRelease],
) -> str | None:
    if pack_row.resolved_install_spec and "appium_driver_version" in pack_row.resolved_install_spec:
        version = pack_row.resolved_install_spec["appium_driver_version"]
        return str(version) if version is not None else None
    if pack_row.resolved_install_spec:
        appium_driver = pack_row.resolved_install_spec.get("appium_driver")
        if isinstance(appium_driver, dict) and appium_driver:
            version = next(iter(appium_driver.values()))
            return str(version) if version is not None else None
    release = pack_release_map.get((pack_row.pack_id, pack_row.pack_release))
    if release and release.manifest_json:
        recommended = release.manifest_json.get("appium_driver", {}).get("recommended")
        return str(recommended) if recommended is not None else None
    return None


def _installed_driver_version(
    pack_row: HostPackInstallation,
    runtime_map: dict[str, HostRuntimeInstallation],
) -> str | None:
    if not pack_row.runtime_id:
        return None
    runtime = runtime_map.get(pack_row.runtime_id)
    if runtime and runtime.driver_specs:
        version = runtime.driver_specs[0].get("version")
        return str(version) if version is not None else None
    return None


def _compute_drift(
    pack_row: HostPackInstallation,
    pack_release_map: dict[tuple[str, str], DriverPackRelease],
    runtime_map: dict[str, HostRuntimeInstallation],
) -> bool:
    desired = _desired_driver_version(pack_row, pack_release_map)
    installed = _installed_driver_version(pack_row, runtime_map)
    return desired is not None and installed is not None and desired != installed


async def upsert_plugin_status(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    runtime_id: str,
    plugin_name: str,
    version: str,
    status: str,
    blocked_reason: str | None = None,
) -> None:
    """Persist plugin install status keyed by (host_id, runtime_id, plugin_name).

    Inserts a new row if none exists; otherwise updates version, status, and updated_at.
    """
    existing = (
        await session.execute(
            select(HostPluginRuntimeStatus).where(
                HostPluginRuntimeStatus.host_id == host_id,
                HostPluginRuntimeStatus.runtime_id == runtime_id,
                HostPluginRuntimeStatus.plugin_name == plugin_name,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        session.add(
            HostPluginRuntimeStatus(
                host_id=host_id,
                runtime_id=runtime_id,
                plugin_name=plugin_name,
                version=version,
                status=status,
                blocked_reason=blocked_reason,
            )
        )
    else:
        existing.version = version
        existing.status = status
        existing.blocked_reason = blocked_reason
