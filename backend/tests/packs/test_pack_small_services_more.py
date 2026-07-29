import io
import tarfile
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.packs.services import capability as pack_capability_service
from app.packs.services import ingest as pack_ingest_service

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _StubSessionFactory:
    @asynccontextmanager
    async def begin(self) -> AsyncIterator[object]:
        yield object()


def _tar_with_member(name: str, data: bytes = b"content", *, member_type: bytes = tarfile.REGTYPE) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.type = member_type
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _tar_with_members(names: list[str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in names:
            data = b"content"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_pack_ingest_archive_validation_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(pack_ingest_service.PackIngestValidationError, match="unsafe archive path"):
        pack_ingest_service._safe_archive_path("../manifest.yaml")

    link = tarfile.TarInfo("manifest.yaml")
    link.type = tarfile.SYMTYPE
    with pytest.raises(pack_ingest_service.PackIngestValidationError, match="unsupported archive member"):
        pack_ingest_service._validate_archive_member(link)

    huge = tarfile.TarInfo("manifest.yaml")
    huge.size = pack_ingest_service.MAX_PACK_MANIFEST_BYTES + 1
    with (
        tarfile.open(fileobj=io.BytesIO(_tar_with_member("other.txt")), mode="r:gz") as tar,
        pytest.raises(pack_ingest_service.PackIngestValidationError, match=r"regular file|maximum size"),
    ):
        pack_ingest_service._extract_limited_manifest(tar, huge)

    monkeypatch.setattr(pack_ingest_service, "MAX_PACK_TARBALL_BYTES", 1)
    with pytest.raises(pack_ingest_service.PackIngestValidationError, match="tarball exceeds"):
        pack_ingest_service._extract_manifest_text(b"too large")

    monkeypatch.setattr(pack_ingest_service, "MAX_PACK_TARBALL_BYTES", 1024)
    with pytest.raises(pack_ingest_service.PackIngestValidationError, match="missing manifest"):
        pack_ingest_service._extract_manifest_text(_tar_with_member("nested/manifest.yaml"))


def test_pack_ingest_rejects_more_archive_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    negative = tarfile.TarInfo("manifest.yaml")
    negative.size = -1
    with pytest.raises(pack_ingest_service.PackIngestValidationError, match="invalid archive member size"):
        pack_ingest_service._validate_archive_member(negative)

    directory = _tar_with_member("manifest.yaml", member_type=tarfile.DIRTYPE)
    with pytest.raises(pack_ingest_service.PackIngestValidationError, match="regular file"):
        pack_ingest_service._extract_manifest_text(directory)

    monkeypatch.setattr(pack_ingest_service, "MAX_PACK_TARBALL_MEMBERS", 1)
    with pytest.raises(pack_ingest_service.PackIngestValidationError, match="too many archive members"):
        pack_ingest_service._extract_manifest_text(_tar_with_members(["manifest.yaml", "other.txt"]))


def test_pack_ingest_rejects_invalid_tarball_and_uncompressed_size(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(pack_ingest_service.PackIngestValidationError, match="invalid tarball"):
        pack_ingest_service._extract_manifest_text(b"not a tarball")

    monkeypatch.setattr(pack_ingest_service, "MAX_PACK_UNCOMPRESSED_BYTES", 1)
    with pytest.raises(pack_ingest_service.PackIngestValidationError, match="uncompressed size exceeds"):
        pack_ingest_service._extract_manifest_text(_tar_with_member("manifest.yaml", b"toolong"))


def test_pack_ingest_manifest_extraction_read_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    member = tarfile.TarInfo("manifest.yaml")
    member.size = 1

    class MissingFileTar:
        def extractfile(self, _member: tarfile.TarInfo) -> None:
            return None

    with pytest.raises(pack_ingest_service.PackIngestValidationError, match="not extractable"):
        pack_ingest_service._extract_limited_manifest(MissingFileTar(), member)  # type: ignore[arg-type]

    class BigHandle(io.BytesIO):
        def __enter__(self) -> BigHandle:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class BigReadTar:
        def extractfile(self, _member: tarfile.TarInfo) -> BigHandle:
            return BigHandle(b"xx")

    monkeypatch.setattr(pack_ingest_service, "MAX_PACK_MANIFEST_BYTES", 1)
    with pytest.raises(pack_ingest_service.PackIngestValidationError, match=r"manifest.yaml exceeds"):
        pack_ingest_service._extract_limited_manifest(BigReadTar(), member)  # type: ignore[arg-type]


async def test_pack_ingest_existing_release_storage_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _tar_with_member("manifest.yaml", b"schema_version: 1\nid: test-pack\nrelease: 1\nplatforms: []\n")
    manifest = SimpleNamespace(
        id="test-pack",
        release="1",
        display_name="Test",
        maintainer=None,
        license=None,
        platforms=[],
        features={},
        insecure_features=[],
    )
    existing = SimpleNamespace(artifact_sha256="different", artifact_path=None)
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: existing))
    monkeypatch.setattr(pack_ingest_service, "load_manifest_yaml", lambda text: manifest)

    with pytest.raises(pack_ingest_service.PackIngestConflictError, match="different content"):
        await pack_ingest_service.reserve_pack_upload(
            session,
            storage=MagicMock(),
            parsed=pack_ingest_service.parse_pack_tarball(data),
        )


async def test_pack_ingest_existing_release_restores_missing_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _tar_with_member("manifest.yaml", b"schema_version: 1\nid: test-pack\nrelease: 1\nplatforms: []\n")
    payload_sha = pack_ingest_service.hashlib.sha256(data).hexdigest()
    manifest = SimpleNamespace(
        id="test-pack",
        release="1",
        display_name="Test",
        maintainer=None,
        license=None,
        platforms=[],
        features={},
        insecure_features=[],
    )
    result = SimpleNamespace(id="test-pack", current_release="1")
    monkeypatch.setattr(pack_ingest_service, "load_manifest_yaml", lambda text: manifest)
    monkeypatch.setattr(
        pack_ingest_service,
        "reserve_pack_upload",
        AsyncMock(return_value=pack_ingest_service.ArtifactReservation("/tmp/restored.tgz", True)),
    )
    activate = AsyncMock(return_value=result)
    monkeypatch.setattr(pack_ingest_service, "activate_pack_upload", activate)
    storage = MagicMock()
    storage.store.return_value = SimpleNamespace(path="/tmp/restored.tgz", sha256=payload_sha, size=len(data))

    result = await pack_ingest_service.ingest_pack_tarball(
        _StubSessionFactory(),
        storage=storage,
        username="u",
        origin_filename="pack.tgz",
        data=data,
    )

    assert result.current_release == "1"
    activate.assert_awaited_once()


async def test_pack_ingest_existing_release_storage_error_becomes_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _tar_with_member("manifest.yaml", b"schema_version: 1\nid: test-pack\nrelease: 1\nplatforms: []\n")
    manifest = SimpleNamespace(
        id="test-pack",
        release="1",
        display_name="Test",
        maintainer=None,
        license=None,
        platforms=[],
        features={},
        insecure_features=[],
    )
    monkeypatch.setattr(pack_ingest_service, "load_manifest_yaml", lambda text: manifest)
    monkeypatch.setattr(
        pack_ingest_service,
        "reserve_pack_upload",
        AsyncMock(return_value=pack_ingest_service.ArtifactReservation("/tmp/restored.tgz", True)),
    )
    storage = MagicMock()
    storage.store.side_effect = pack_ingest_service.PackStorageError("disk full")

    with pytest.raises(pack_ingest_service.PackIngestConflictError, match="disk full"):
        await pack_ingest_service.ingest_pack_tarball(
            _StubSessionFactory(),
            storage=storage,
            username="u",
            origin_filename="pack.tgz",
            data=data,
        )


async def test_pack_ingest_wraps_manifest_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _tar_with_member("manifest.yaml", b"not valid")
    monkeypatch.setattr(
        pack_ingest_service,
        "load_manifest_yaml",
        MagicMock(side_effect=pack_ingest_service.ManifestValidationError("bad manifest")),
    )

    with pytest.raises(pack_ingest_service.PackIngestValidationError, match="bad manifest"):
        await pack_ingest_service.ingest_pack_tarball(
            MagicMock(),
            storage=MagicMock(),
            username="u",
            origin_filename="pack.tgz",
            data=data,
        )


async def test_pack_ingest_new_release_storage_and_manifest_dict_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _tar_with_member("manifest.yaml", b"schema_version: 1\nid: test-pack\nrelease: 1\nplatforms: []\n")
    manifest = SimpleNamespace(
        id="test-pack",
        release="1",
        display_name="Test",
        maintainer=None,
        license=None,
        platforms=[],
        features={},
        insecure_features=[],
    )
    monkeypatch.setattr(pack_ingest_service, "load_manifest_yaml", lambda text: manifest)
    monkeypatch.setattr(
        pack_ingest_service,
        "reserve_pack_upload",
        AsyncMock(return_value=pack_ingest_service.ArtifactReservation("/tmp/pack.tgz", True)),
    )

    storage = MagicMock()
    storage.store.side_effect = pack_ingest_service.PackStorageError("disk full")
    with pytest.raises(pack_ingest_service.PackIngestConflictError, match="disk full"):
        await pack_ingest_service.ingest_pack_tarball(
            _StubSessionFactory(),
            storage=storage,
            username="u",
            origin_filename="pack.tgz",
            data=data,
        )

    storage.store.side_effect = None
    storage.store.return_value = SimpleNamespace(path="/tmp/pack.tgz", sha256="sha", size=len(data))
    monkeypatch.setattr(pack_ingest_service.yaml, "safe_load", lambda _text: ["bad"])
    with pytest.raises(pack_ingest_service.PackIngestValidationError, match="dictionary"):
        await pack_ingest_service.ingest_pack_tarball(
            _StubSessionFactory(),
            storage=storage,
            username="u",
            origin_filename="pack.tgz",
            data=data,
        )


async def test_pack_ingest_existing_pack_without_release_adds_new_release(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _tar_with_member("manifest.yaml", b"schema_version: 1\nid: test-pack\nrelease: 2\nplatforms: []\n")
    manifest = SimpleNamespace(
        id="test-pack",
        release="2",
        display_name="Test",
        maintainer=None,
        license=None,
        platforms=[],
        features={},
        insecure_features=[],
    )
    existing = SimpleNamespace(id="test-pack", current_release="2")
    monkeypatch.setattr(pack_ingest_service, "load_manifest_yaml", lambda text: manifest)
    monkeypatch.setattr(
        pack_ingest_service,
        "reserve_pack_upload",
        AsyncMock(return_value=pack_ingest_service.ArtifactReservation("/tmp/pack.tgz", True)),
    )
    activate = AsyncMock(return_value=existing)
    monkeypatch.setattr(pack_ingest_service, "activate_pack_upload", activate)
    storage = MagicMock()
    storage.store.return_value = SimpleNamespace(path="/tmp/pack.tgz", sha256="sha", size=len(data))

    result = await pack_ingest_service.ingest_pack_tarball(
        _StubSessionFactory(),
        storage=storage,
        username="u",
        origin_filename="pack.tgz",
        data=data,
    )

    assert result is existing
    assert existing.current_release == "2"
    activate.assert_awaited_once()


async def test_pack_capability_rendering_edges() -> None:
    session = MagicMock()
    session.scalars = AsyncMock(
        return_value=MagicMock(unique=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
    )
    session.scalar = AsyncMock(return_value=None)
    # Empty catalog means the pack row itself is absent; the message must say so
    # rather than claiming the pack exists with no releases.
    with pytest.raises(LookupError, match="unknown pack missing"):
        await pack_capability_service.render_stereotype(session, pack_id="missing", platform_id="android")

    assert (
        await pack_capability_service.resolve_appium_env(
            session,
            pack_id="missing",
            platform_id="android",
            device_type="real_device",
            os_version="14",
        )
        == {}
    )

    resolved = SimpleNamespace(
        default_capabilities={
            "good": "device-{device.connection_target}",
            "badPrefix": "{host.name}",
            "missing": "{device.udid}",
            "literal": 3,
        },
        device_fields_schema=[
            {"id": "udid", "capability_name": "appium:udid"},
            {"id": "ignored"},
        ],
    )
    assert pack_capability_service.render_default_capabilities(
        resolved,
        device_context={"connection_target": "abc"},
    ) == {"good": "device-abc", "literal": 3}
    assert pack_capability_service.render_device_field_capabilities(resolved, {"udid": "abc"}) == {"appium:udid": "abc"}
    assert pack_capability_service._semver_ge("14.1.2", "14.1") is True
