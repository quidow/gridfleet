from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.host_pack_installation import HostPackDoctorResult, HostPackInstallation
from app.models.host_plugin_runtime_status import HostPluginRuntimeStatus
from app.models.host_runtime_installation import HostRuntimeInstallation
from app.services import pack_status_service
from tests.pack.factories import seed_test_packs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host


async def test_apply_status_updates_existing_runtime_pack_and_plugin(db_session: AsyncSession, db_host: Host) -> None:
    await seed_test_packs(db_session)
    runtime = HostRuntimeInstallation(
        host_id=db_host.id,
        runtime_id="rt-existing",
        appium_server_package="appium",
        appium_server_version="2.0.0",
        driver_specs=[],
        plugin_specs=None,
        appium_home="/old",
        status="pending",
        blocked_reason="old",
    )
    pack = HostPackInstallation(
        host_id=db_host.id,
        pack_id="appium-uiautomator2",
        pack_release="2026.04.0",
        runtime_id="rt-existing",
        status="pending",
    )
    plugin = HostPluginRuntimeStatus(
        host_id=db_host.id,
        runtime_id="rt-existing",
        plugin_name="images",
        version="1.0.0",
        status="pending",
    )
    db_session.add_all([runtime, pack, plugin])
    await db_session.commit()

    await pack_status_service.apply_status(
        db_session,
        {
            "host_id": str(db_host.id),
            "runtimes": [
                {
                    "runtime_id": "rt-existing",
                    "appium_server": {"package": "appium", "version": "2.19.0"},
                    "appium_driver": [{"package": "uiautomator2", "version": "5.0.0"}],
                    "appium_plugins": [
                        {"name": "images", "version": "2.0.0", "status": "installed", "blocked_reason": None}
                    ],
                    "appium_home": "/new",
                    "status": "installed",
                    "blocked_reason": None,
                }
            ],
            "packs": [
                {
                    "pack_id": "appium-uiautomator2",
                    "pack_release": "2026.05.0",
                    "runtime_id": "rt-existing",
                    "status": "installed",
                    "resolved_install_spec": {"appium_driver": {"uiautomator2": "5.0.0"}},
                    "installer_log_excerpt": "ok",
                    "resolver_version": "2",
                    "blocked_reason": None,
                }
            ],
            "doctor": [
                {"pack_id": "appium-uiautomator2", "check_id": "node", "ok": True, "message": "ok"},
                {"pack_id": "appium-xcuitest", "check_id": "ignored", "ok": False, "message": "ignored"},
            ],
        },
    )
    await db_session.commit()

    await db_session.refresh(runtime)
    await db_session.refresh(pack)
    await db_session.refresh(plugin)
    assert runtime.appium_server_version == "2.19.0"
    assert runtime.plugin_specs == []
    assert runtime.appium_home == "/new"
    assert pack.pack_release == "2026.05.0"
    assert pack.installed_at is not None
    assert plugin.version == "2.0.0"
    doctors = (await db_session.execute(select(HostPackDoctorResult))).scalars().all()
    assert [(row.pack_id, row.check_id) for row in doctors] == [("appium-uiautomator2", "node")]


async def test_pack_status_helper_fallbacks(db_session: AsyncSession, db_host: Host) -> None:
    pack = HostPackInstallation(
        host_id=db_host.id,
        pack_id="unknown-pack",
        pack_release="1",
        runtime_id=None,
        status="blocked",
        resolved_install_spec={"appium_driver_version": None},
    )
    assert pack_status_service._pack_row_supports_host(pack, db_host, {}) is True
    assert pack_status_service._desired_driver_version(pack, {}) is None
    assert pack_status_service._installed_driver_version(pack, {}) is None
    assert pack_status_service._compute_drift(pack, {}, {}) is False

    pack.resolved_install_spec = {"appium_driver": {"uiautomator2": "5.0.0"}}
    assert pack_status_service._desired_driver_version(pack, {}) == "5.0.0"
    pack.resolved_install_spec = {}
    release = type("Release", (), {"manifest_json": {"appium_driver": {"recommended": "6.0.0"}}})()
    assert pack_status_service._desired_driver_version(pack, {(pack.pack_id, pack.pack_release): release}) == "6.0.0"

    runtime = HostRuntimeInstallation(
        host_id=db_host.id,
        runtime_id="rt",
        driver_specs=[{"version": "5.0.0"}],
    )
    pack.runtime_id = "rt"
    assert pack_status_service._installed_driver_version(pack, {"rt": runtime}) == "5.0.0"
