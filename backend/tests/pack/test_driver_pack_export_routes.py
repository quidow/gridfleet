"""Tests for driver_pack_export router — Step 1 (TDD: failing tests first).

Route under test:
    POST /api/driver-packs/{pack_id}/releases/{release}/export

Cases:
  - test_export_route_returns_tarball_with_sha_header
  - test_export_route_404_on_missing
  - test_export_route_anonymous_rejected_when_auth_enabled
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from typing import TYPE_CHECKING

import pytest

from app.config import settings as process_settings
from app.main import app
from app.routers.driver_pack_export import get_pack_storage
from app.services.pack_storage_service import PackStorageService

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MANIFEST_YAML = """\
schema_version: 1
id: export-test-pack
release: 1.0.0
display_name: Export Test Pack
appium_server: { source: npm, package: appium, version: ">=2.5,<3", recommended: 2.19.0 }
appium_driver: { source: npm, package: appium-export-test-driver, version: ">=0,<1", recommended: 0.1.0 }
platforms:
  - id: export_p
    display_name: Export Platform
    automation_name: ExportAutomation
    appium_platform_name: Export
    device_types: [real_device]
    connection_types: [network]
    grid_slots: [native]
    capabilities: { stereotype: {}, session_required: [] }
    identity: { scheme: export_uid, scope: global }
"""


def _build_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        body = _MANIFEST_YAML.encode()
        info = tarfile.TarInfo(name="manifest.yaml")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def override_storage(tmp_path: Path) -> Iterator[None]:
    """Override the export router's storage dependency to use a writable tmp_path."""

    def _tmp_storage() -> PackStorageService:
        return PackStorageService(root=tmp_path)

    app.dependency_overrides[get_pack_storage] = _tmp_storage
    yield
    app.dependency_overrides.pop(get_pack_storage, None)


@pytest.fixture
def auth_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Enable auth and provide credentials so anonymous calls return 401/403."""
    values = {
        "auth_username": "operator",
        "auth_password": "operator-secret",
        "auth_session_secret": "session-secret-for-tests",
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_export_route_returns_tarball_with_sha_header(client: AsyncClient, tmp_path: Path) -> None:
    """POST /export returns gzip bytes with X-Pack-Sha256 header and Content-Disposition."""
    # First upload the pack so it exists in DB with artifact_path set
    tarball = _build_tarball()
    files = {"tarball": ("export-test-pack-1.0.0.tar.gz", tarball, "application/gzip")}

    # Reuse the upload route to create the pack with stored artifact
    from app.routers.driver_pack_uploads import get_pack_storage as upload_get_storage

    # Override the upload storage to use the same tmp_path
    app.dependency_overrides[upload_get_storage] = lambda: PackStorageService(root=tmp_path)
    try:
        upload_res = await client.post("/api/driver-packs/uploads", files=files)
        assert upload_res.status_code == 201, upload_res.text
    finally:
        app.dependency_overrides.pop(upload_get_storage, None)

    # Now test the export endpoint
    res = await client.post("/api/driver-packs/export-test-pack/releases/1.0.0/export")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/gzip"

    sha_header = res.headers.get("x-pack-sha256")
    assert sha_header is not None
    assert len(sha_header) == 64  # hex sha256

    # Verify sha matches the returned bytes
    assert sha_header == hashlib.sha256(res.content).hexdigest()

    # Content-Disposition must be present
    disposition = res.headers.get("content-disposition", "")
    assert "attachment" in disposition
    assert ".tar.gz" in disposition


async def test_export_route_404_on_missing(client: AsyncClient) -> None:
    """POST /export returns 404 when pack/release does not exist."""
    res = await client.post("/api/driver-packs/no-such-pack/releases/0.0.0/export")
    assert res.status_code == 404


async def test_export_route_anonymous_rejected_when_auth_enabled(
    auth_settings: Iterator[None],
    client: AsyncClient,
) -> None:
    """POST /export returns 401 or 403 for unauthenticated callers when auth is enabled."""
    res = await client.post("/api/driver-packs/some-pack/releases/1.0.0/export")
    assert res.status_code in (401, 403)
