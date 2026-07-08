from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

from sqlalchemy import select

from app.packs.models import HostPackDoctorResult, HostPackInstallation
from app.packs.services import status as pack_status_service
from app.packs.services.driver_version import desired_driver_version, has_driver_drift, installed_driver_version
from app.packs.services.feature_dispatch import FeatureService
from app.packs.services.status import PackStatusService
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs

_feature_svc = FeatureService(publisher=event_bus, circuit_breaker=Mock())
_status_svc = PackStatusService(feature=_feature_svc)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


async def test_apply_status_updates_existing_runtime_pack_and_plugin(db_session: AsyncSession, db_host: Host) -> None:
    await seed_test_packs(db_session)
    pack = HostPackInstallation(
        host_id=db_host.id,
        pack_id="appium-uiautomator2",
        pack_release="2026.04.0",
        runtime_id="rt-existing",
        status="pending",
        appium_server_package="appium",
        appium_server_version="2.0.0",
        driver_specs=[],
        appium_home="/old",
        runtime_status="pending",
        runtime_blocked_reason="old",
    )
    db_session.add(pack)
    await db_session.commit()

    await _status_svc.apply_status(
        db_session,
        {
            "host_id": str(db_host.id),
            "runtimes": [
                {
                    "runtime_id": "rt-existing",
                    "appium_server": {"package": "appium", "version": "2.19.0"},
                    "appium_driver": [{"package": "uiautomator2", "version": "5.0.0"}],
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

    await db_session.refresh(pack)
    assert pack.appium_server_version == "2.19.0"
    assert pack.appium_home == "/new"
    assert pack.runtime_status == "installed"
    assert pack.pack_release == "2026.05.0"
    assert pack.installed_at is not None
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
    assert desired_driver_version(pack, None) is None
    assert installed_driver_version(None) is None
    assert has_driver_drift(pack, None) is False

    pack.resolved_install_spec = {"appium_driver": {"uiautomator2": "5.0.0"}}
    assert desired_driver_version(pack, None) == "5.0.0"
    pack.resolved_install_spec = {}
    release = type("Release", (), {"manifest_json": {"appium_driver": {"recommended": "6.0.0"}}})()
    assert desired_driver_version(pack, release) == "6.0.0"

    pack.driver_specs = [{"version": "5.0.0"}]
    assert installed_driver_version(pack) == "5.0.0"
