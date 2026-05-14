from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.core.config import settings as process_settings
from app.devices.models import ConnectionType, Device, DeviceType
from app.main import app
from app.packs.models import (
    DriverPack,
    DriverPackRelease,
    HostPackDoctorResult,
    HostPackFeatureStatus,
    HostPackInstallation,
)
from app.packs.routers.uploads import get_pack_storage
from app.packs.services.storage import PackStorageService
from app.packs.services.upload import MAX_PACK_TARBALL_BYTES

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.asyncio

_MANIFEST_YAML = """\
schema_version: 1
id: vendor-foo
release: __RELEASE__
display_name: Vendor Foo
appium_server: { source: npm, package: appium, version: ">=2.5,<3", recommended: 2.19.0 }
appium_driver: { source: npm, package: appium-vendor-foo-driver, version: ">=0,<1", recommended: 0.1.0 }
platforms:
  - id: vendor_p
    display_name: Vendor
    automation_name: VendorAutomation
    appium_platform_name: Vendor
    device_types: [real_device]
    connection_types: [network]
    grid_slots: [native]
    capabilities: { stereotype: {}, session_required: [] }
    identity: { scheme: vendor_uid, scope: global }
"""


def _manifest(release: str = "0.1.0") -> str:
    return _MANIFEST_YAML.replace("__RELEASE__", release)


def _tarball(release: str = "0.1.0") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        body = _manifest(release).encode()
        info = tarfile.TarInfo(name="manifest.yaml")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


@pytest.fixture(autouse=True)
def override_storage(tmp_path: Path) -> Iterator[None]:
    """Override the pack storage dependency to use a writable tmp_path."""

    def _tmp_storage() -> PackStorageService:
        return PackStorageService(root=tmp_path)

    app.dependency_overrides[get_pack_storage] = _tmp_storage
    yield
    # Clean up only our override; the conftest clears all overrides after the
    # client fixture, but we clean ours here defensively in case client fixture
    # teardown has already run.
    app.dependency_overrides.pop(get_pack_storage, None)


@pytest.fixture
def auth_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Enable auth and provide credentials so anonymous calls return 401/403."""
    values = {
        "auth_username": "operator",
        "auth_password": "operator-secret",
        "auth_session_secret": "session-secret-for-tests-pad-to-32-bytes",
        "machine_auth_username": "machine",
        "machine_auth_password": "machine-secret",
    }
    monkeypatch.setattr(process_settings, "auth_enabled", True)
    monkeypatch.setattr(process_settings, "auth_username", values["auth_username"])
    monkeypatch.setattr(process_settings, "auth_password", values["auth_password"])
    monkeypatch.setattr(process_settings, "auth_session_secret", values["auth_session_secret"])
    monkeypatch.setattr(process_settings, "auth_session_ttl_sec", 28_800)
    monkeypatch.setattr(process_settings, "auth_cookie_secure", False)
    monkeypatch.setattr(process_settings, "machine_auth_username", values["machine_auth_username"])
    monkeypatch.setattr(process_settings, "machine_auth_password", values["machine_auth_password"])
    yield values


async def test_upload_route_persists_pack(client: AsyncClient) -> None:
    files = {"tarball": ("vendor-foo-0.1.0.tar.gz", _tarball(), "application/gzip")}
    res = await client.post("/api/driver-packs/uploads", files=files)
    assert res.status_code == 201
    body = res.json()
    assert body["id"] == "vendor-foo"
    assert "origin" not in body


async def test_tarball_fetch_returns_bytes(client: AsyncClient) -> None:
    tarball = _tarball()
    files = {"tarball": ("vendor-foo-0.1.0.tar.gz", tarball, "application/gzip")}
    await client.post("/api/driver-packs/uploads", files=files)
    res = await client.get("/api/driver-packs/vendor-foo/releases/0.1.0/tarball")
    assert res.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(res.content), mode="r:gz") as tar:
        member = tar.getmember("manifest.yaml")
        extracted = tar.extractfile(member)
        assert extracted is not None
        assert extracted.read() == _manifest().encode()


async def test_reupload_same_release_restores_missing_artifact(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    tarball = _tarball()
    files = {"tarball": ("vendor-foo-0.1.0.tar.gz", tarball, "application/gzip")}
    await client.post("/api/driver-packs/uploads", files=files)
    release = (
        await db_session.execute(
            select(DriverPackRelease).where(
                DriverPackRelease.pack_id == "vendor-foo",
                DriverPackRelease.release == "0.1.0",
            )
        )
    ).scalar_one()
    assert release.artifact_path is not None
    Path(release.artifact_path).unlink()

    res = await client.post("/api/driver-packs/uploads", files=files)

    assert res.status_code == 201
    fetch_res = await client.get("/api/driver-packs/vendor-foo/releases/0.1.0/tarball")
    assert fetch_res.status_code == 200
    assert fetch_res.content == tarball


async def test_tarball_fetch_404_when_artifact_file_missing(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    files = {"tarball": ("vendor-foo-0.1.0.tar.gz", _tarball(), "application/gzip")}
    await client.post("/api/driver-packs/uploads", files=files)
    release = (
        await db_session.execute(
            select(DriverPackRelease).where(
                DriverPackRelease.pack_id == "vendor-foo",
                DriverPackRelease.release == "0.1.0",
            )
        )
    ).scalar_one()
    assert release.artifact_path is not None
    Path(release.artifact_path).unlink()

    res = await client.get("/api/driver-packs/vendor-foo/releases/0.1.0/tarball")

    assert res.status_code == 404
    assert "release artifact not found" in res.json()["error"]["message"]


async def test_tarball_fetch_404_for_unknown_release(client: AsyncClient) -> None:
    res = await client.get("/api/driver-packs/missing/releases/0.0.0/tarball")
    assert res.status_code == 404


async def test_list_pack_releases_marks_current(client: AsyncClient) -> None:
    await client.post(
        "/api/driver-packs/uploads",
        files={"tarball": ("vendor-foo-0.1.0.tar.gz", _tarball("0.1.0"), "application/gzip")},
    )
    await client.post(
        "/api/driver-packs/uploads",
        files={"tarball": ("vendor-foo-0.2.0.tar.gz", _tarball("0.2.0"), "application/gzip")},
    )

    res = await client.get("/api/driver-packs/vendor-foo/releases")

    assert res.status_code == 200
    body = res.json()
    assert body["pack_id"] == "vendor-foo"
    assert [release["release"] for release in body["releases"]] == ["0.2.0", "0.1.0"]
    assert [release["is_current"] for release in body["releases"]] == [True, False]


async def test_switch_pack_current_release_updates_catalog_and_desired_state(
    client: AsyncClient,
    db_host: Host,
) -> None:
    await client.post(
        "/api/driver-packs/uploads",
        files={"tarball": ("vendor-foo-0.1.0.tar.gz", _tarball("0.1.0"), "application/gzip")},
    )
    await client.post(
        "/api/driver-packs/uploads",
        files={"tarball": ("vendor-foo-0.2.0.tar.gz", _tarball("0.2.0"), "application/gzip")},
    )

    res = await client.patch("/api/driver-packs/vendor-foo/releases/current", json={"release": "0.1.0"})

    assert res.status_code == 200
    assert res.json()["current_release"] == "0.1.0"
    releases = (await client.get("/api/driver-packs/vendor-foo/releases")).json()["releases"]
    assert [release["release"] for release in releases] == ["0.2.0", "0.1.0"]
    assert [release["is_current"] for release in releases] == [False, True]
    desired = (await client.get("/agent/driver-packs/desired", params={"host_id": str(db_host.id)})).json()
    pack = next(pack for pack in desired["packs"] if pack["id"] == "vendor-foo")
    assert pack["release"] == "0.1.0"


async def test_switch_pack_current_release_rejects_unknown_release(client: AsyncClient) -> None:
    await client.post(
        "/api/driver-packs/uploads",
        files={"tarball": ("vendor-foo-0.1.0.tar.gz", _tarball("0.1.0"), "application/gzip")},
    )

    res = await client.patch("/api/driver-packs/vendor-foo/releases/current", json={"release": "9.9.9"})

    assert res.status_code == 404


async def test_delete_pack_release_removes_non_current_release_and_artifact(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await client.post(
        "/api/driver-packs/uploads",
        files={"tarball": ("vendor-foo-0.1.0.tar.gz", _tarball("0.1.0"), "application/gzip")},
    )
    await client.post(
        "/api/driver-packs/uploads",
        files={"tarball": ("vendor-foo-0.2.0.tar.gz", _tarball("0.2.0"), "application/gzip")},
    )
    release = (
        await db_session.execute(
            select(DriverPackRelease).where(
                DriverPackRelease.pack_id == "vendor-foo",
                DriverPackRelease.release == "0.1.0",
            )
        )
    ).scalar_one()
    artifact_path = release.artifact_path
    assert artifact_path is not None

    res = await client.delete("/api/driver-packs/vendor-foo/releases/0.1.0")

    assert res.status_code == 204
    remaining = (
        (await db_session.execute(select(DriverPackRelease).where(DriverPackRelease.pack_id == "vendor-foo")))
        .scalars()
        .all()
    )
    assert [row.release for row in remaining] == ["0.2.0"]
    with pytest.raises(FileNotFoundError):
        open(artifact_path, "rb").close()


async def test_delete_pack_release_rejects_only_release(client: AsyncClient) -> None:
    await client.post(
        "/api/driver-packs/uploads",
        files={"tarball": ("vendor-foo-0.1.0.tar.gz", _tarball("0.1.0"), "application/gzip")},
    )

    res = await client.delete("/api/driver-packs/vendor-foo/releases/0.1.0")

    assert res.status_code == 400
    assert "only release" in res.json()["error"]["message"]


async def test_delete_pack_release_rejects_host_installed_release(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    await client.post(
        "/api/driver-packs/uploads",
        files={"tarball": ("vendor-foo-0.1.0.tar.gz", _tarball("0.1.0"), "application/gzip")},
    )
    await client.post(
        "/api/driver-packs/uploads",
        files={"tarball": ("vendor-foo-0.2.0.tar.gz", _tarball("0.2.0"), "application/gzip")},
    )
    db_session.add(
        HostPackInstallation(
            host_id=db_host.id,
            pack_id="vendor-foo",
            pack_release="0.1.0",
            status="installed",
        )
    )
    await db_session.commit()

    res = await client.delete("/api/driver-packs/vendor-foo/releases/0.1.0")

    assert res.status_code == 409
    assert "installed on 1 host" in res.json()["error"]["message"]


async def test_delete_driver_pack_removes_installed_pack_and_artifacts(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    await client.post(
        "/api/driver-packs/uploads",
        files={"tarball": ("vendor-foo-0.1.0.tar.gz", _tarball("0.1.0"), "application/gzip")},
    )
    release = (
        await db_session.execute(
            select(DriverPackRelease).where(
                DriverPackRelease.pack_id == "vendor-foo",
                DriverPackRelease.release == "0.1.0",
            )
        )
    ).scalar_one()
    artifact_path = release.artifact_path
    assert artifact_path is not None
    db_session.add_all(
        [
            HostPackInstallation(
                host_id=db_host.id,
                pack_id="vendor-foo",
                pack_release="0.1.0",
                status="installed",
            ),
            HostPackDoctorResult(
                host_id=db_host.id,
                pack_id="vendor-foo",
                check_id="doctor",
                ok=True,
                message="ok",
            ),
            HostPackFeatureStatus(
                host_id=db_host.id,
                pack_id="vendor-foo",
                feature_id="feature",
                ok=True,
                detail="ok",
            ),
        ]
    )
    await db_session.commit()

    res = await client.delete("/api/driver-packs/vendor-foo")

    assert res.status_code == 204
    assert await db_session.get(DriverPack, "vendor-foo") is None
    assert (
        await db_session.scalar(select(HostPackInstallation).where(HostPackInstallation.pack_id == "vendor-foo"))
    ) is None
    assert (
        await db_session.scalar(select(HostPackDoctorResult).where(HostPackDoctorResult.pack_id == "vendor-foo"))
    ) is None
    assert (
        await db_session.scalar(select(HostPackFeatureStatus).where(HostPackFeatureStatus.pack_id == "vendor-foo"))
    ) is None
    assert not Path(artifact_path).exists()


async def test_delete_driver_pack_rejects_pack_with_devices(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    await client.post(
        "/api/driver-packs/uploads",
        files={"tarball": ("vendor-foo-0.1.0.tar.gz", _tarball("0.1.0"), "application/gzip")},
    )
    db_session.add(
        Device(
            name="Vendor Device",
            pack_id="vendor-foo",
            platform_id="vendor_p",
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.network,
            host_id=db_host.id,
            os_version="1.0",
            identity_scheme="vendor_uid",
            identity_scope="global",
            identity_value="vendor-device-1",
        )
    )
    await db_session.commit()

    res = await client.delete("/api/driver-packs/vendor-foo")

    assert res.status_code == 409
    assert "1 device" in res.json()["error"]["message"]


async def test_anonymous_caller_rejected_when_auth_enabled(
    auth_settings: Iterator[None],
    client: AsyncClient,
) -> None:
    res = await client.post("/api/driver-packs/uploads")
    assert res.status_code in (401, 403)


async def test_upload_route_rejects_oversized_tarball(client: AsyncClient) -> None:
    files = {"tarball": ("huge.tar.gz", b"x" * (MAX_PACK_TARBALL_BYTES + 1), "application/gzip")}

    res = await client.post("/api/driver-packs/uploads", files=files)

    assert res.status_code == 413
    assert "tarball exceeds maximum size" in res.json()["error"]["message"]
