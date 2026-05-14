from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.hosts.models import Host, HostStatus, OSType
from app.packs.models import DriverPack, DriverPackRelease, HostPackInstallation, HostRuntimeInstallation
from app.packs.schemas import HostPackStatusOut
from app.packs.services.status import get_host_driver_pack_status


def test_host_pack_status_includes_driver_version_fields() -> None:
    status = HostPackStatusOut(
        pack_id="test-pack",
        pack_release="1.0.0",
        runtime_id="abc123",
        status="installed",
        resolved_install_spec=None,
        installer_log_excerpt=None,
        resolver_version=None,
        blocked_reason=None,
        installed_at=None,
        desired_appium_driver_version="3.6.0",
        installed_appium_driver_version="3.5.0",
        appium_driver_drift=True,
    )
    assert status.desired_appium_driver_version == "3.6.0"
    assert status.installed_appium_driver_version == "3.5.0"
    assert status.appium_driver_drift is True


def test_drift_defaults_false() -> None:
    status = HostPackStatusOut(
        pack_id="test-pack",
        pack_release="1.0.0",
        runtime_id=None,
        status="pending",
        resolved_install_spec=None,
        installer_log_excerpt=None,
        resolver_version=None,
        blocked_reason=None,
        installed_at=None,
    )
    assert status.appium_driver_drift is False
    assert status.desired_appium_driver_version is None
    assert status.installed_appium_driver_version is None


async def test_drift_detected_when_installed_differs_from_desired(db_session) -> None:  # noqa: ANN001
    host_id = uuid.uuid4()
    db_session.add(
        Host(
            id=host_id,
            hostname="test-host",
            ip="127.0.0.1",
            os_type=OSType.linux,
            agent_port=5100,
            status=HostStatus.online,
        )
    )
    db_session.add(DriverPack(id="test-pack", origin="uploaded", display_name="Test", state="enabled"))
    await db_session.flush()
    db_session.add(
        DriverPackRelease(
            pack_id="test-pack",
            release="1.0.0",
            manifest_json={
                "schema_version": 1,
                "id": "test-pack",
                "release": "1.0.0",
                "display_name": "Test",
                "appium_server": {
                    "source": "npm",
                    "package": "appium",
                    "version": ">=2.5,<3",
                    "recommended": "2.11.5",
                },
                "appium_driver": {
                    "source": "npm",
                    "package": "appium-uiautomator2-driver",
                    "version": ">=3,<5",
                    "recommended": "3.6.0",
                },
                "platforms": [],
            },
        )
    )
    db_session.add(
        HostRuntimeInstallation(
            host_id=host_id,
            runtime_id="rt-abc",
            appium_server_package="appium",
            appium_server_version="2.11.5",
            driver_specs=[{"package": "appium-uiautomator2-driver", "version": "3.5.0"}],
            status="installed",
        )
    )
    db_session.add(
        HostPackInstallation(
            host_id=host_id,
            pack_id="test-pack",
            pack_release="1.0.0",
            runtime_id="rt-abc",
            status="installed",
            installed_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    result = await get_host_driver_pack_status(db_session, host_id)
    pack_status = result["packs"][0]
    assert pack_status["desired_appium_driver_version"] == "3.6.0"
    assert pack_status["installed_appium_driver_version"] == "3.5.0"
    assert pack_status["appium_driver_drift"] is True
