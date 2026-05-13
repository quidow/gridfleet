from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.models.driver_pack import DriverPack, DriverPackPlatform, DriverPackRelease
from app.models.host import Host, HostStatus, OSType
from app.models.host_pack_installation import HostPackDoctorResult, HostPackInstallation
from app.schemas.driver_pack import RuntimePolicy
from app.services import pack_delete_service, pack_policy_service, pack_release_service
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_pack_with_releases(
    db_session: AsyncSession,
    tmp_path: Path,
    *,
    pack_id: str = "local/coverage-pack",
) -> tuple[DriverPack, DriverPackRelease, DriverPackRelease, Path, Path]:
    old_artifact = tmp_path / "old.tgz"
    new_artifact = tmp_path / "new.tgz"
    old_artifact.write_text("old")
    new_artifact.write_text("new")
    pack = DriverPack(
        id=pack_id,
        origin="uploaded",
        display_name="Coverage Pack",
        current_release="2.0.0",
        runtime_policy={"strategy": "recommended"},
    )
    old_release = DriverPackRelease(
        pack_id=pack_id,
        release="1.0.0",
        manifest_json={"id": pack_id, "release": "1.0.0"},
        artifact_path=str(old_artifact),
        artifact_sha256="old-sha",
    )
    new_release = DriverPackRelease(
        pack_id=pack_id,
        release="2.0.0",
        manifest_json={"id": pack_id, "release": "2.0.0"},
        artifact_path=str(new_artifact),
        artifact_sha256="new-sha",
    )
    pack.releases.extend([old_release, new_release])
    db_session.add(pack)
    await db_session.flush()
    db_session.add(
        DriverPackPlatform(
            pack_release_id=old_release.id,
            manifest_platform_id="android_mobile",
            display_name="Android",
            automation_name="UiAutomator2",
            appium_platform_name="Android",
            device_types=["real_device"],
            connection_types=["usb"],
            grid_slots=["default"],
            data={"identity": {"scheme": "android_serial", "scope": "host"}},
        )
    )
    db_session.add(
        DriverPackPlatform(
            pack_release_id=new_release.id,
            manifest_platform_id="android_mobile",
            display_name="Android",
            automation_name="UiAutomator2",
            appium_platform_name="Android",
            device_types=["real_device"],
            connection_types=["usb"],
            grid_slots=["default"],
            data={"identity": {"scheme": "android_serial", "scope": "host"}},
        )
    )
    await db_session.commit()
    await db_session.refresh(pack)
    return pack, old_release, new_release, old_artifact, new_artifact


async def _seed_host(db_session: AsyncSession, *, hostname: str = "pack-host") -> Host:
    host = Host(hostname=hostname, ip="10.40.0.1", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()
    return host


async def test_list_and_set_current_release(db_session: AsyncSession, tmp_path: Path) -> None:
    await _seed_pack_with_releases(db_session, tmp_path)

    releases = await pack_release_service.list_releases(db_session, "local/coverage-pack")
    assert releases is not None
    assert releases.pack_id == "local/coverage-pack"
    assert [(release.release, release.is_current) for release in releases.releases] == [
        ("2.0.0", True),
        ("1.0.0", False),
    ]
    assert releases.releases[0].platform_ids == ["android_mobile"]

    assert await pack_release_service.list_releases(db_session, "missing/pack") is None
    with pytest.raises(LookupError):
        await pack_release_service.set_current_release(db_session, "missing/pack", "1.0.0")
    with pytest.raises(LookupError):
        await pack_release_service.set_current_release(db_session, "local/coverage-pack", "9.9.9")

    pack = await pack_release_service.set_current_release(db_session, "local/coverage-pack", "1.0.0")
    assert pack.current_release == "1.0.0"


async def test_delete_release_guards_installed_and_orphaned_platforms(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    pack, _old_release, _new_release, _old_artifact, _new_artifact = await _seed_pack_with_releases(
        db_session, tmp_path
    )
    pack_id = pack.id
    old_release_name = "1.0.0"
    old_release_id = (
        await db_session.execute(
            select(DriverPackRelease.id).where(
                DriverPackRelease.pack_id == pack_id,
                DriverPackRelease.release == old_release_name,
            )
        )
    ).scalar_one()
    host = await _seed_host(db_session)
    db_session.add(
        HostPackInstallation(host_id=host.id, pack_id=pack_id, pack_release=old_release_name, status="installed")
    )
    await db_session.commit()

    with pytest.raises(RuntimeError, match="installed on 1 host"):
        await pack_release_service.delete_release(db_session, pack_id, old_release_name)

    installation = (
        await db_session.execute(select(HostPackInstallation).where(HostPackInstallation.pack_id == pack_id))
    ).scalar_one()
    await db_session.delete(installation)
    await db_session.flush()
    db_session.add(
        DriverPackPlatform(
            pack_release_id=old_release_id,
            manifest_platform_id="roku_network",
            display_name="Roku",
            automation_name="Roku",
            appium_platform_name="Roku",
            device_types=["real_device"],
            connection_types=["network"],
            grid_slots=["default"],
            data={},
        )
    )
    await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="192.0.2.1",
        name="Roku",
        pack_id=pack_id,
        platform_id="roku_network",
        identity_scheme="ip",
        connection_type="network",
    )
    db_session.expire_all()

    with pytest.raises(RuntimeError, match="only present in that release"):
        await pack_release_service.delete_release(db_session, pack_id, old_release_name)


async def test_delete_current_release_advances_current_and_unlinks_artifact(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    pack, _old_release, _new_release, _old_artifact, new_artifact = await _seed_pack_with_releases(db_session, tmp_path)

    await pack_release_service.delete_release(db_session, pack.id, "2.0.0")
    await db_session.commit()

    reloaded = await db_session.get(DriverPack, pack.id)
    assert reloaded is not None
    assert reloaded.current_release == "1.0.0"
    assert new_artifact.exists() is False


async def test_delete_release_rejects_missing_and_only_release(db_session: AsyncSession, tmp_path: Path) -> None:
    pack, _old_release, _new_release, _old_artifact, _new_artifact = await _seed_pack_with_releases(
        db_session, tmp_path
    )
    pack_id = pack.id

    with pytest.raises(LookupError):
        await pack_release_service.delete_release(db_session, "missing/pack", "1.0.0")
    with pytest.raises(LookupError):
        await pack_release_service.delete_release(db_session, pack_id, "9.9.9")

    await pack_release_service.delete_release(db_session, pack_id, "2.0.0")
    await db_session.commit()
    db_session.expire_all()

    with pytest.raises(ValueError, match="only release"):
        await pack_release_service.delete_release(db_session, pack_id, "1.0.0")


async def test_delete_pack_guards_references_then_removes_pack_side_tables(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    pack, _old_release, _new_release, old_artifact, new_artifact = await _seed_pack_with_releases(db_session, tmp_path)
    host = await _seed_host(db_session)
    device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="pack-delete-device",
        name="Pack Delete Device",
        pack_id=pack.id,
    )

    with pytest.raises(RuntimeError, match="1 device still use it"):
        await pack_delete_service.delete_pack(db_session, pack.id)

    await db_session.delete(device)
    db_session.add_all(
        [
            HostPackInstallation(host_id=host.id, pack_id=pack.id, pack_release="2.0.0", status="installed"),
            HostPackDoctorResult(host_id=host.id, pack_id=pack.id, check_id="doctor", ok=True, message="ok"),
        ]
    )
    await db_session.commit()

    await pack_delete_service.delete_pack(db_session, pack.id)
    await db_session.commit()

    assert await db_session.get(DriverPack, pack.id) is None
    assert old_artifact.exists() is False
    assert new_artifact.exists() is False


async def test_delete_pack_missing_and_runtime_policy_update(db_session: AsyncSession, tmp_path: Path) -> None:
    await _seed_pack_with_releases(db_session, tmp_path)

    with pytest.raises(LookupError):
        await pack_delete_service.delete_pack(db_session, "missing/pack")
    with pytest.raises(LookupError):
        await pack_policy_service.set_runtime_policy(db_session, "missing/pack", RuntimePolicy())

    policy = RuntimePolicy(
        strategy="exact",
        appium_server_version="2.10.0",
        appium_driver_version="3.0.0",
    )
    pack = await pack_policy_service.set_runtime_policy(db_session, "local/coverage-pack", policy)
    assert pack.runtime_policy == {
        "strategy": "exact",
        "appium_server_version": "2.10.0",
        "appium_driver_version": "3.0.0",
    }
