from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.models.driver_pack import (
    DriverPack,
    DriverPackFeature,
    DriverPackPlatform,
    DriverPackRelease,
)
from app.models.host_pack_installation import (
    HostPackDoctorResult,
    HostPackInstallation,
)
from app.models.host_plugin_runtime_status import HostPluginRuntimeStatus
from app.models.host_runtime_installation import HostRuntimeInstallation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host


@pytest.mark.asyncio
async def test_insert_and_load_driver_pack(db_session: AsyncSession) -> None:
    pack = DriverPack(id="test-pack", origin="uploaded", display_name="Test Pack")
    db_session.add(pack)
    await db_session.commit()

    rows = (await db_session.execute(select(DriverPack).where(DriverPack.id == "test-pack"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].origin == "uploaded"
    assert rows[0].display_name == "Test Pack"
    assert rows[0].maintainer == ""
    assert rows[0].license == ""
    assert rows[0].state == "enabled"


@pytest.mark.asyncio
async def test_driver_pack_defaults_runtime_policy(db_session: AsyncSession) -> None:
    pack = DriverPack(
        id="policy-pack",
        origin="uploaded",
        display_name="Policy Pack",
        maintainer="tests",
        license="Apache-2.0",
    )
    db_session.add(pack)
    await db_session.commit()

    row = await db_session.get(DriverPack, "policy-pack")

    assert row is not None
    assert row.runtime_policy == {"strategy": "recommended"}


@pytest.mark.asyncio
async def test_plugin_runtime_status_stores_blocked_reason(db_session: AsyncSession, db_host: Host) -> None:
    status = HostPluginRuntimeStatus(
        host_id=db_host.id,
        runtime_id="runtime-1",
        plugin_name="images",
        version="1.0.0",
        status="blocked",
        blocked_reason="plugin_install_failed: peer dependency mismatch",
    )
    db_session.add(status)
    await db_session.commit()

    rows = (await db_session.execute(select(HostPluginRuntimeStatus))).scalars().all()

    assert rows[0].blocked_reason == "plugin_install_failed: peer dependency mismatch"


@pytest.mark.asyncio
async def test_driver_pack_release_with_pack(db_session: AsyncSession) -> None:
    pack = DriverPack(id="test-pack", origin="uploaded", display_name="Test Pack")
    release = DriverPackRelease(
        pack_id="test-pack",
        release="1.0.0",
        manifest_json={"id": "test-pack", "version": "1.0.0"},
    )
    db_session.add_all([pack, release])
    await db_session.commit()

    rows = (
        (await db_session.execute(select(DriverPackRelease).where(DriverPackRelease.pack_id == "test-pack")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].release == "1.0.0"
    assert rows[0].manifest_json == {"id": "test-pack", "version": "1.0.0"}


@pytest.mark.asyncio
async def test_driver_pack_platform(db_session: AsyncSession) -> None:
    pack = DriverPack(id="test-pack", origin="uploaded", display_name="Test Pack")
    release = DriverPackRelease(
        pack_id="test-pack",
        release="1.0.0",
        manifest_json={"id": "test-pack", "version": "1.0.0"},
    )
    db_session.add_all([pack, release])
    await db_session.flush()

    platform = DriverPackPlatform(
        pack_release_id=release.id,
        manifest_platform_id="android-platform",
        display_name="Android Platform",
        automation_name="UiAutomator2",
        appium_platform_name="android",
        device_types=["real_device", "emulator"],
        connection_types=["usb", "network"],
        grid_slots=["android_mobile"],
        data={"example": "data"},
    )
    db_session.add(platform)
    await db_session.commit()

    rows = (
        (await db_session.execute(select(DriverPackPlatform).where(DriverPackPlatform.pack_release_id == release.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].manifest_platform_id == "android-platform"
    assert rows[0].device_types == ["real_device", "emulator"]
    assert rows[0].connection_types == ["usb", "network"]
    assert rows[0].grid_slots == ["android_mobile"]


@pytest.mark.asyncio
async def test_driver_pack_feature(db_session: AsyncSession) -> None:
    pack = DriverPack(id="test-pack", origin="uploaded", display_name="Test Pack")
    release = DriverPackRelease(
        pack_id="test-pack",
        release="1.0.0",
        manifest_json={"id": "test-pack", "version": "1.0.0"},
    )
    db_session.add_all([pack, release])
    await db_session.flush()

    feature = DriverPackFeature(
        pack_release_id=release.id,
        manifest_feature_id="feature-1",
        data={"name": "Feature 1", "enabled": True},
    )
    db_session.add(feature)
    await db_session.commit()

    rows = (
        (await db_session.execute(select(DriverPackFeature).where(DriverPackFeature.pack_release_id == release.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].manifest_feature_id == "feature-1"
    assert rows[0].data == {"name": "Feature 1", "enabled": True}


@pytest.mark.asyncio
async def test_host_runtime_installation(db_session: AsyncSession, db_host: Host) -> None:
    runtime_install = HostRuntimeInstallation(
        host_id=db_host.id,
        runtime_id="appium-runtime",
        appium_server_package="appium",
        appium_server_version="2.0.0",
        driver_specs=[
            {"driver": "uiautomator2", "version": "1.0.0"},
            {"driver": "xcuitest", "version": "1.0.0"},
        ],
    )
    db_session.add(runtime_install)
    await db_session.commit()

    rows = (
        (await db_session.execute(select(HostRuntimeInstallation).where(HostRuntimeInstallation.host_id == db_host.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].runtime_id == "appium-runtime"
    assert rows[0].appium_server_version == "2.0.0"
    assert len(rows[0].driver_specs) == 2
    assert rows[0].plugin_specs == []
    assert rows[0].refcount == 0
    assert rows[0].status == "pending"


@pytest.mark.asyncio
async def test_host_pack_installation(db_session: AsyncSession, db_host: Host) -> None:
    pack = DriverPack(id="test-pack", origin="uploaded", display_name="Test Pack")
    pack_install = HostPackInstallation(
        host_id=db_host.id,
        pack_id="test-pack",
        pack_release="1.0.0",
    )
    db_session.add_all([pack, pack_install])
    await db_session.commit()

    rows = (
        (await db_session.execute(select(HostPackInstallation).where(HostPackInstallation.host_id == db_host.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].pack_id == "test-pack"
    assert rows[0].pack_release == "1.0.0"
    assert rows[0].status == "pending"
    assert rows[0].runtime_id is None


@pytest.mark.asyncio
async def test_host_pack_doctor_result(db_session: AsyncSession, db_host: Host) -> None:
    pack = DriverPack(id="test-pack", origin="uploaded", display_name="Test Pack")
    doctor_result = HostPackDoctorResult(
        host_id=db_host.id,
        pack_id="test-pack",
        check_id="check-health",
        ok=True,
        message="All checks passed",
    )
    db_session.add_all([pack, doctor_result])
    await db_session.commit()

    rows = (
        (await db_session.execute(select(HostPackDoctorResult).where(HostPackDoctorResult.host_id == db_host.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].pack_id == "test-pack"
    assert rows[0].check_id == "check-health"
    assert rows[0].ok is True
    assert rows[0].message == "All checks passed"
