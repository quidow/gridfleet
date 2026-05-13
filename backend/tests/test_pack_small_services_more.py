import io
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.driver_pack import PackState
from app.services import pack_capability_service, pack_ingest_service


def _tar_with_member(name: str, data: bytes = b"content", *, member_type: bytes = tarfile.REGTYPE) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.type = member_type
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


async def test_pack_ingest_existing_release_storage_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _tar_with_member("manifest.yaml", b"schema_version: 1\nid: test-pack\nrelease: 1\nplatforms: []\n")
    manifest = SimpleNamespace(
        id="test-pack",
        release="1",
        display_name="Test",
        maintainer=None,
        license=None,
        derived_from=None,
        template_id=None,
        platforms=[],
        features={},
    )
    release = SimpleNamespace(release="1", artifact_sha256="different", artifact_path=None)
    existing = SimpleNamespace(
        id="test-pack",
        current_release=None,
        releases=[release],
        is_runnable=True,
        state=PackState.enabled,
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: existing))
    monkeypatch.setattr(pack_ingest_service, "load_manifest_yaml", lambda text: manifest)

    with pytest.raises(pack_ingest_service.PackIngestConflictError, match="different content"):
        await pack_ingest_service.ingest_pack_tarball(
            session,
            storage=MagicMock(),
            username="u",
            origin_filename="pack.tgz",
            data=data,
        )


async def test_pack_capability_rendering_edges() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    with pytest.raises(LookupError, match="no releases"):
        await pack_capability_service.render_stereotype(session, pack_id="missing", platform_id="android")

    assert (
        await pack_capability_service.resolve_workaround_env(
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
