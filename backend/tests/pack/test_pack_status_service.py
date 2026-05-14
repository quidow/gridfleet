"""Unit tests for pack_status_service.apply_status.

Verifies that when the agent posts a mixed payload (one pack installed, one
blocked with a specific error string), each pack lands correctly in the DB,
and the installed pack is not affected by the blocked one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from app.hosts.models import Host, HostPluginRuntimeStatus, HostStatus, OSType
from app.packs.models import HostPackDoctorResult, HostPackFeatureStatus, HostPackInstallation, HostRuntimeInstallation
from app.packs.services import status as pack_status_service
from app.packs.services.status import apply_status, get_host_driver_pack_status
from tests.pack.factories import seed_test_packs


@pytest.mark.asyncio
async def test_apply_status_one_installed_one_blocked(db_session: AsyncSession) -> None:
    """One pack installed + one blocked with blocked_reason each land correctly."""
    # Seed packs so FK constraint on host_pack_installations.pack_id is satisfied.
    await seed_test_packs(db_session)

    host = Host(
        hostname="h-status-test.local",
        ip="10.0.0.99",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.commit()
    host_id = str(host.id)

    payload = {
        "host_id": host_id,
        "runtimes": [
            {
                "runtime_id": "runtime-ok",
                "appium_server": {"package": "appium", "version": "2.19.0"},
                "appium_driver": [{"package": "appium-uiautomator2-driver", "version": "5.0.0"}],
                "appium_plugins": [],
                "appium_home": "/runtimes/runtime-ok",
                "status": "installed",
                "blocked_reason": None,
            }
        ],
        "packs": [
            {
                "pack_id": "appium-uiautomator2",
                "pack_release": "2026.04.0",
                "runtime_id": "runtime-ok",
                "status": "installed",
                "resolved_install_spec": {"appium": "2.19.0", "uiautomator2": "5.0.0"},
                "installer_log_excerpt": "",
                "resolver_version": "1",
                "blocked_reason": None,
            },
            {
                "pack_id": "appium-xcuitest",
                "pack_release": "2026.04.0",
                "runtime_id": None,
                "status": "blocked",
                "resolved_install_spec": None,
                "installer_log_excerpt": None,
                "resolver_version": None,
                "blocked_reason": "driver install failed for xcuitest-driver",
            },
        ],
        "doctor": [],
    }

    await apply_status(db_session, payload)
    await db_session.commit()

    installs = (await db_session.execute(select(HostPackInstallation))).scalars().all()
    by_pack = {i.pack_id: i for i in installs}

    assert "appium-uiautomator2" in by_pack
    assert by_pack["appium-uiautomator2"].status == "installed"
    assert by_pack["appium-uiautomator2"].runtime_id == "runtime-ok"
    assert by_pack["appium-uiautomator2"].blocked_reason is None

    assert "appium-xcuitest" in by_pack
    assert by_pack["appium-xcuitest"].status == "blocked"
    assert by_pack["appium-xcuitest"].runtime_id is None
    assert by_pack["appium-xcuitest"].blocked_reason == "driver install failed for xcuitest-driver"

    runtimes = (await db_session.execute(select(HostRuntimeInstallation))).scalars().all()
    assert len(runtimes) == 1
    assert runtimes[0].runtime_id == "runtime-ok"
    assert runtimes[0].status == "installed"


@pytest.mark.asyncio
async def test_apply_status_persists_plugin_status_per_runtime(db_session: AsyncSession, db_host: Host) -> None:
    await apply_status(
        db_session,
        {
            "host_id": str(db_host.id),
            "runtimes": [
                {
                    "runtime_id": "runtime-1",
                    "appium_server": {"package": "appium", "version": "2.11.5"},
                    "appium_driver": [{"package": "appium-uiautomator2-driver", "version": "3.6.0"}],
                    "appium_plugins": [
                        {
                            "name": "images",
                            "version": "1.0.0",
                            "source": "npm:appium-plugin-images",
                            "package": None,
                            "status": "blocked",
                            "blocked_reason": "plugin_install_failed: peer dependency mismatch",
                        }
                    ],
                    "appium_home": "/tmp/runtime-1",
                    "status": "installed",
                    "blocked_reason": None,
                }
            ],
            "packs": [],
            "doctor": [],
        },
    )
    await db_session.commit()

    rows = (await db_session.execute(select(HostPluginRuntimeStatus))).scalars().all()

    assert len(rows) == 1
    assert rows[0].runtime_id == "runtime-1"
    assert rows[0].plugin_name == "images"
    assert rows[0].status == "blocked"
    assert rows[0].blocked_reason == "plugin_install_failed: peer dependency mismatch"


@pytest.mark.asyncio
async def test_apply_status_persists_sidecar_feature_status(db_session: AsyncSession, db_host: Host) -> None:
    await apply_status(
        db_session,
        {
            "host_id": str(db_host.id),
            "runtimes": [],
            "packs": [],
            "doctor": [],
            "sidecars": [
                {
                    "pack_id": "uploaded-sidecar-pack",
                    "release": "1.0.0",
                    "feature_id": "tunnel",
                    "ok": False,
                    "detail": "tunnel down",
                    "state": "failed",
                    "last_error": "boom",
                }
            ],
        },
    )
    await db_session.commit()

    row = (await db_session.execute(select(HostPackFeatureStatus))).scalar_one()
    assert row.host_id == db_host.id
    assert row.pack_id == "uploaded-sidecar-pack"
    assert row.feature_id == "tunnel"
    assert row.ok is False
    assert row.detail == "tunnel down"


@pytest.mark.asyncio
async def test_host_driver_pack_status_returns_feature_status(db_session: AsyncSession, db_host: Host) -> None:
    await apply_status(
        db_session,
        {
            "host_id": str(db_host.id),
            "runtimes": [],
            "packs": [],
            "doctor": [],
            "sidecars": [
                {
                    "pack_id": "uploaded-sidecar-pack",
                    "release": "1.0.0",
                    "feature_id": "tunnel",
                    "ok": True,
                    "detail": "running",
                    "state": "running",
                    "last_error": None,
                }
            ],
        },
    )
    await db_session.commit()

    payload = await get_host_driver_pack_status(db_session, db_host.id)

    assert payload["features"] == [
        {
            "pack_id": "uploaded-sidecar-pack",
            "feature_id": "tunnel",
            "ok": True,
            "detail": "running",
        }
    ]


@pytest.mark.asyncio
async def test_host_driver_pack_status_omits_incompatible_pack_rows(db_session: AsyncSession, db_host: Host) -> None:
    await seed_test_packs(db_session)
    db_session.add(
        HostPackInstallation(
            host_id=db_host.id,
            pack_id="appium-xcuitest",
            pack_release="2026.04.12",
            runtime_id=None,
            status="blocked",
            blocked_reason="Xcode is required",
        )
    )
    await db_session.commit()

    payload = await get_host_driver_pack_status(db_session, db_host.id)

    assert payload["packs"] == []


@pytest.mark.asyncio
async def test_driver_pack_host_status_omits_incompatible_hosts(db_session: AsyncSession, db_host: Host) -> None:
    await seed_test_packs(db_session)
    db_session.add(
        HostPackInstallation(
            host_id=db_host.id,
            pack_id="appium-xcuitest",
            pack_release="2026.04.12",
            runtime_id=None,
            status="blocked",
            blocked_reason="Xcode is required",
        )
    )
    await db_session.commit()

    payload = await pack_status_service.get_driver_pack_host_status(db_session, "appium-xcuitest")

    assert payload["hosts"] == []


@pytest.mark.asyncio
async def test_driver_pack_host_status_returns_pack_rows_with_runtime_and_doctor(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    host = Host(
        hostname="mac-status-test.local",
        ip="10.0.0.98",
        os_type=OSType.macos,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()
    runtime = HostRuntimeInstallation(
        host_id=host.id,
        runtime_id="runtime-xcuitest",
        appium_server_package="appium",
        appium_server_version="2.19.0",
        driver_specs=[{"package": "appium-xcuitest-driver", "version": "9.1.0"}],
        plugin_specs=[],
        appium_home="/opt/gridfleet-agent/runtimes/runtime-xcuitest",
        status="installed",
        blocked_reason=None,
    )
    pack = HostPackInstallation(
        host_id=host.id,
        pack_id="appium-xcuitest",
        pack_release="2026.04.0",
        runtime_id="runtime-xcuitest",
        status="installed",
        resolved_install_spec={"appium_driver_version": "9.3.1"},
        installer_log_excerpt="installed xcuitest",
        resolver_version="resolver-1",
        blocked_reason=None,
    )
    doctor = HostPackDoctorResult(
        host_id=host.id,
        pack_id="appium-xcuitest",
        check_id="xcode",
        ok=False,
        message="Xcode missing",
    )
    db_session.add_all([runtime, pack, doctor])
    await db_session.commit()

    payload = await pack_status_service.get_driver_pack_host_status(db_session, "appium-xcuitest")

    assert payload["pack_id"] == "appium-xcuitest"
    assert payload["hosts"] == [
        {
            "host_id": str(host.id),
            "hostname": host.hostname,
            "status": host.status,
            "pack_release": "2026.04.0",
            "runtime_id": "runtime-xcuitest",
            "pack_status": "installed",
            "resolved_install_spec": {"appium_driver_version": "9.3.1"},
            "installer_log_excerpt": "installed xcuitest",
            "resolver_version": "resolver-1",
            "blocked_reason": None,
            "installed_at": pack.installed_at,
            "desired_appium_driver_version": "9.3.1",
            "installed_appium_driver_version": "9.1.0",
            "appium_driver_drift": True,
            "appium_home": "/opt/gridfleet-agent/runtimes/runtime-xcuitest",
            "runtime_status": "installed",
            "runtime_blocked_reason": None,
            "appium_server_version": "2.19.0",
            "doctor": [{"check_id": "xcode", "ok": False, "message": "Xcode missing"}],
        }
    ]


@pytest.mark.asyncio
async def test_apply_status_clears_stale_doctor_rows_when_pack_reports_empty(db_session: AsyncSession) -> None:
    """A pack reported with an empty doctor list must wipe its pre-existing doctor rows."""
    await seed_test_packs(db_session)

    host = Host(
        hostname="h-doctor-reconcile.local",
        ip="10.0.0.42",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.commit()

    db_session.add(
        HostPackDoctorResult(
            host_id=host.id,
            pack_id="appium-uiautomator2",
            check_id="adb",
            ok=False,
            message="stale parse error",
        )
    )
    await db_session.commit()

    payload = {
        "host_id": str(host.id),
        "runtimes": [],
        "packs": [
            {
                "pack_id": "appium-uiautomator2",
                "pack_release": "2026.04.0",
                "runtime_id": None,
                "status": "installed",
                "resolved_install_spec": None,
                "installer_log_excerpt": None,
                "resolver_version": None,
                "blocked_reason": None,
            }
        ],
        "doctor": [],
    }

    await apply_status(db_session, payload)
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(HostPackDoctorResult).where(
                    HostPackDoctorResult.host_id == host.id,
                    HostPackDoctorResult.pack_id == "appium-uiautomator2",
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_apply_status_ignores_doctor_entries_for_unreported_packs(db_session: AsyncSession) -> None:
    """Doctor entries for pack_ids absent from the packs list are silently dropped."""
    await seed_test_packs(db_session)

    host = Host(
        hostname="h-doctor-stray.local",
        ip="10.0.0.43",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.commit()

    payload = {
        "host_id": str(host.id),
        "runtimes": [],
        "packs": [
            {
                "pack_id": "appium-uiautomator2",
                "pack_release": "2026.04.0",
                "runtime_id": None,
                "status": "installed",
                "resolved_install_spec": None,
                "installer_log_excerpt": None,
                "resolver_version": None,
                "blocked_reason": None,
            }
        ],
        "doctor": [
            {"pack_id": "appium-uiautomator2", "check_id": "adb", "ok": True, "message": ""},
            {"pack_id": "appium-xcuitest", "check_id": "xcrun", "ok": True, "message": ""},
        ],
    }

    await apply_status(db_session, payload)
    await db_session.commit()

    rows = (
        (await db_session.execute(select(HostPackDoctorResult).where(HostPackDoctorResult.host_id == host.id)))
        .scalars()
        .all()
    )
    pack_ids = {row.pack_id for row in rows}
    assert pack_ids == {"appium-uiautomator2"}


@pytest.mark.asyncio
async def test_apply_status_preserves_doctor_rows_for_blocked_packs(db_session: AsyncSession) -> None:
    """Blocked packs did not run doctor; previously-recorded rows must not be wiped."""
    await seed_test_packs(db_session)

    host = Host(
        hostname="h-doctor-blocked.local",
        ip="10.0.0.44",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.commit()

    db_session.add(
        HostPackDoctorResult(
            host_id=host.id,
            pack_id="appium-uiautomator2",
            check_id="adb",
            ok=True,
            message="last good check",
        )
    )
    await db_session.commit()

    payload = {
        "host_id": str(host.id),
        "runtimes": [],
        "packs": [
            {
                "pack_id": "appium-uiautomator2",
                "pack_release": "2026.04.0",
                "runtime_id": None,
                "status": "blocked",
                "resolved_install_spec": None,
                "installer_log_excerpt": None,
                "resolver_version": None,
                "blocked_reason": "runtime install failed",
            }
        ],
        "doctor": [],
    }

    await apply_status(db_session, payload)
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(HostPackDoctorResult).where(
                    HostPackDoctorResult.host_id == host.id,
                    HostPackDoctorResult.pack_id == "appium-uiautomator2",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].check_id == "adb"
    assert rows[0].message == "last good check"
