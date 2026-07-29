from __future__ import annotations

import hashlib
import io
import tarfile
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError

from app.auth import auth_settings as process_settings
from app.devices.models import ConnectionType, Device, DeviceType
from app.events.models import SystemEvent
from app.main import app
from app.packs.dependencies import get_pack_services
from app.packs.models import (
    DriverPack,
    DriverPackRelease,
    HostPackDoctorResult,
    HostPackInstallation,
    PackArtifact,
)
from app.packs.services import service as pack_service
from app.packs.services.ingest import MAX_PACK_TARBALL_BYTES

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host
    from app.packs.services_container import PackServices

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


@pytest.fixture
def pack_storage_root(tmp_path: Path) -> Path:
    """Route pack storage to a per-test writable directory."""
    return tmp_path


class _ObservedPackSessions:
    """Wraps the pack container's factory so a test can see its transactions.

    Two things the routes owe their callers are only observable from here: that
    a mutation runs inside a factory-owned transaction at all, and that the
    filesystem work happens once that transaction has ended. ``fail_before_exit``
    injects the failure as a real statement against a table that does not exist,
    so the transaction is genuinely aborted rather than left artificially clean.

    Only ``begin()`` is wrapped: no pack route takes the plain ``session_factory()``
    read form, so a stand-in for it would be scaffolding nothing could keep honest.
    """

    def __init__(self) -> None:
        self._inner: async_sessionmaker[AsyncSession] | None = None
        self.sessions: list[AsyncSession] = []
        self.fail_before_exit = False

    def bind(self, inner: async_sessionmaker[AsyncSession]) -> None:
        """Adopt the factory the container built for this request."""
        self._inner = inner

    @property
    def _bound(self) -> async_sessionmaker[AsyncSession]:
        assert self._inner is not None, "no request has resolved the pack container yet"
        return self._inner

    async def _inject(self, db: AsyncSession) -> None:
        if self.fail_before_exit:
            await db.execute(text("SELECT 1 FROM gridfleet_no_such_table"))

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        async with self._bound.begin() as db:
            self.sessions.append(db)
            yield db
            await self._inject(db)

    @property
    def open_transactions(self) -> int:
        return sum(1 for session in self.sessions if session.in_transaction())


@pytest.fixture
def observed_pack_sessions(client: AsyncClient) -> Iterator[_ObservedPackSessions]:
    del client  # ordering only: the client fixture installs the override this wraps
    base = app.dependency_overrides[get_pack_services]
    observed = _ObservedPackSessions()

    def _override() -> PackServices:
        services = base()
        observed.bind(services.session_factory)
        return replace(services, session_factory=observed)  # type: ignore[arg-type]

    app.dependency_overrides[get_pack_services] = _override
    yield observed
    app.dependency_overrides[get_pack_services] = base


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


async def test_reupload_emits_only_for_new_or_restored_artifacts(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    tarball = _tarball()
    files = {"tarball": ("vendor-foo-0.1.0.tar.gz", tarball, "application/gzip")}

    assert (await client.post("/api/driver-packs/uploads", files=files)).status_code == 201
    assert (await client.post("/api/driver-packs/uploads", files=files)).status_code == 201

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

    assert (await client.post("/api/driver-packs/uploads", files=files)).status_code == 201

    events = (
        (
            await db_session.execute(
                select(SystemEvent).where(SystemEvent.type == "driver_pack.upload").order_by(SystemEvent.id)
            )
        )
        .scalars()
        .all()
    )
    payload = {
        "uploaded_by": "anonymous-admin",
        "pack_id": "vendor-foo",
        "release": "0.1.0",
        "artifact_sha256": hashlib.sha256(tarball).hexdigest(),
        "origin_filename": "vendor-foo-0.1.0.tar.gz",
    }
    assert [event.data for event in events] == [payload, payload]


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


async def _upload(client: AsyncClient, release: str) -> None:
    res = await client.post(
        "/api/driver-packs/uploads",
        files={"tarball": (f"vendor-foo-{release}.tar.gz", _tarball(release), "application/gzip")},
    )
    assert res.status_code == 201, res.text


def _watch_unlinks(monkeypatch: pytest.MonkeyPatch, observed: _ObservedPackSessions) -> list[int]:
    """Record, per ``Path.unlink`` call, how many pack transactions were open.

    Armed after the uploads that seed a test, so the spy covers only the two
    deletion paths — the upload path deliberately writes its artifact inside its
    transaction.
    """
    open_at_unlink: list[int] = []
    real_unlink = Path.unlink

    def _spy(self: Path, *args: object, **kwargs: object) -> None:
        open_at_unlink.append(observed.open_transactions)
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", _spy)
    return open_at_unlink


def _watch_artifact_writes(monkeypatch: pytest.MonkeyPatch, observed: _ObservedPackSessions) -> list[int]:
    """Record, per ``Path.write_bytes`` call, how many pack transactions were open.

    The upload twin of ``_watch_unlinks``. A lexical scan cannot see this: the
    write happens inside ``PackStorageService.store``, several frames below the
    route that owns the boundaries, so only a real session watching a real write
    can prove the bytes move with nothing open.
    """
    open_at_write: list[int] = []
    real_write_bytes = Path.write_bytes

    def _spy(self: Path, data: bytes) -> int:
        open_at_write.append(observed.open_transactions)
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", _spy)
    return open_at_write


async def test_upload_writes_the_artifact_with_no_open_transaction(
    client: AsyncClient,
    observed_pack_sessions: _ObservedPackSessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_at_write = _watch_artifact_writes(monkeypatch, observed_pack_sessions)

    res = await client.post(
        "/api/driver-packs/uploads",
        files={"tarball": ("vendor-foo-0.1.0.tar.gz", _tarball("0.1.0"), "application/gzip")},
    )

    assert res.status_code == 201, res.text
    assert len(observed_pack_sessions.sessions) == 2, (
        "the upload must own a reserve transaction and an activate transaction, "
        f"got {len(observed_pack_sessions.sessions)}"
    )
    assert open_at_write, "the upload never wrote an artifact"
    assert open_at_write == [0] * len(open_at_write), (
        f"artifact bytes were written with {open_at_write} pack transaction(s) open; "
        "no transaction may span the storage write"
    )


async def test_delete_pack_unlinks_artifacts_after_its_transaction(
    client: AsyncClient,
    observed_pack_sessions: _ObservedPackSessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _upload(client, "0.1.0")
    open_at_unlink = _watch_unlinks(monkeypatch, observed_pack_sessions)

    res = await client.delete("/api/driver-packs/vendor-foo")

    assert res.status_code == 204
    assert observed_pack_sessions.sessions, "the delete route must own a factory transaction"
    assert open_at_unlink, "the pack's artifact was never unlinked"
    assert open_at_unlink == [0] * len(open_at_unlink), (
        f"artifact deletion ran with {open_at_unlink} pack transaction(s) still open; "
        "no pack lock may span filesystem deletion"
    )


async def test_delete_release_unlinks_artifact_after_its_transaction(
    client: AsyncClient,
    observed_pack_sessions: _ObservedPackSessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _upload(client, "0.1.0")
    await _upload(client, "0.2.0")
    open_at_unlink = _watch_unlinks(monkeypatch, observed_pack_sessions)

    res = await client.delete("/api/driver-packs/vendor-foo/releases/0.1.0")

    assert res.status_code == 204
    assert observed_pack_sessions.sessions, "the delete-release route must own a factory transaction"
    assert open_at_unlink, "the release artifact was never unlinked"
    assert open_at_unlink == [0] * len(open_at_unlink), (
        f"artifact deletion ran with {open_at_unlink} pack transaction(s) still open"
    )


async def test_failed_artifact_deletion_is_logged_and_still_reports_success(
    client: AsyncClient,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unlink is post-commit, so its failure is logged rather than surfaced.

    The deletion the caller asked for is already durable; answering 500 would
    report a rollback that did not happen and send the operator looking for a
    pack that is gone. Read the durability half from a peer session — the request
    session would show the deletion whether or not it was ever committed.

    NOTE: spy on ``logger.warning`` directly instead of going through ``caplog``.
    ``unlink_pack_artifact`` logs through structlog's stdlib bridge, so the record
    has to survive stdlib filtering and propagate to the root handler ``caplog``
    installs, and other tests running in the same xdist worker can leave that
    state in a configuration where the WARNING record never reaches handlers —
    which has produced a flake on CI (see
    ``tests/devices/test_maintenance_service_exit.py``). Spying on the call site
    bypasses the pipeline entirely and verifies the contract directly.
    """
    await _upload(client, "0.1.0")

    def _explode(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError(f"cannot remove {self}")

    monkeypatch.setattr(Path, "unlink", _explode)
    with patch.object(pack_service.logger, "warning") as warning_spy:
        res = await client.delete("/api/driver-packs/vendor-foo")
    monkeypatch.undo()

    assert res.status_code == 204, "a failing unlink must not turn a committed delete into an error"
    assert warning_spy.called, "the orphaned artifact was not reported anywhere"
    warning_args, _ = warning_spy.call_args
    assert warning_args[0] == "pack_artifact_unlink_failed", (
        f"warning message must mention pack_artifact_unlink_failed (got: {warning_args[0]!r})"
    )
    async with db_session_maker() as peer:
        assert await peer.get(DriverPack, "vendor-foo") is None, (
            "metadata deletion committed before the unlink and must stay committed"
        )


async def test_upload_rolls_back_metadata_outbox_and_reservation_together(
    client: AsyncClient,
    db_session: AsyncSession,
    observed_pack_sessions: _ObservedPackSessions,
) -> None:
    """A failed upload leaves no row, no outbox event and no ledger reservation."""
    observed_pack_sessions.fail_before_exit = True

    with pytest.raises(ProgrammingError):
        await client.post(
            "/api/driver-packs/uploads",
            files={"tarball": ("vendor-foo-0.1.0.tar.gz", _tarball("0.1.0"), "application/gzip")},
        )

    assert await db_session.get(DriverPack, "vendor-foo") is None
    assert (await db_session.scalar(select(DriverPackRelease).where(DriverPackRelease.pack_id == "vendor-foo"))) is None
    assert (await db_session.scalar(select(SystemEvent).where(SystemEvent.type == "driver_pack.upload"))) is None, (
        "the outbox row survived a rolled-back upload"
    )
    assert (await db_session.scalars(select(PackArtifact))).all() == [], "the reservation survived a rolled-back upload"


async def test_release_mutations_roll_back_on_a_pre_exit_failure(
    client: AsyncClient,
    db_session: AsyncSession,
    observed_pack_sessions: _ObservedPackSessions,
) -> None:
    await _upload(client, "0.1.0")
    await _upload(client, "0.2.0")
    doomed_release = (
        await db_session.execute(
            select(DriverPackRelease).where(
                DriverPackRelease.pack_id == "vendor-foo",
                DriverPackRelease.release == "0.1.0",
            )
        )
    ).scalar_one()
    artifact_path = doomed_release.artifact_path
    assert artifact_path is not None
    observed_pack_sessions.fail_before_exit = True

    with pytest.raises(ProgrammingError):
        await client.patch("/api/driver-packs/vendor-foo/releases/current", json={"release": "0.1.0"})
    with pytest.raises(ProgrammingError):
        await client.delete("/api/driver-packs/vendor-foo/releases/0.1.0")
    with pytest.raises(ProgrammingError):
        await client.delete("/api/driver-packs/vendor-foo")

    db_session.expire_all()
    pack = await db_session.get(DriverPack, "vendor-foo")
    assert pack is not None, "a failed delete must leave the pack in place"
    assert pack.current_release == "0.2.0", "a failed current-release switch must not stick"
    remaining = (
        (await db_session.execute(select(DriverPackRelease).where(DriverPackRelease.pack_id == "vendor-foo")))
        .scalars()
        .all()
    )
    assert sorted(row.release for row in remaining) == ["0.1.0", "0.2.0"]
    assert Path(artifact_path).is_file(), "a rolled-back delete must not have unlinked the artifact"


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
