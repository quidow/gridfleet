"""Tests that upload_pack populates DriverPackFeature rows from manifest.features."""

from __future__ import annotations

import io
import tarfile
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.packs.models import DriverPackFeature
from app.packs.services.storage import PackStorageService
from app.packs.services.upload import upload_pack

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

_MANIFEST_WITH_FEATURES = """\
schema_version: 1
id: vendor-featured
release: 1.0.0
display_name: Vendor Featured
appium_server:
  source: npm
  package: appium
  version: ">=2.5,<3"
  recommended: 2.19.0
appium_driver:
  source: npm
  package: appium-vendor-featured-driver
  version: ">=0,<1"
  recommended: 0.1.0
platforms:
  - id: vendor_p
    display_name: Vendor Platform
    automation_name: VendorAutomation
    appium_platform_name: Vendor
    device_types: [real_device]
    connection_types: [network]
    grid_slots: [native]
    capabilities: { stereotype: {}, session_required: [] }
    identity: { scheme: vendor_uid, scope: global }
features:
  remote_debug:
    display_name: Remote Debug
    description_md: "Enable ADB-over-Wi-Fi tunnel."
    help_url: https://example.com/docs/remote-debug
    sidecar:
      adapter_hook: start_remote_debug_sidecar
    actions:
      - id: enable
        adapter_hook: enable_remote_debug
        display_name: Enable
      - id: disable
        adapter_hook: disable_remote_debug
        display_name: Disable
  screenshot_service:
    display_name: Screenshot Service
    applies_when:
      platform_ids: [vendor_p]
"""


def _build_tarball(manifest: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        manifest_bytes = manifest.encode()
        info = tarfile.TarInfo(name="manifest.yaml")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_upload_populates_feature_rows(db_session: AsyncSession, tmp_path: Path) -> None:
    storage = PackStorageService(root=tmp_path)
    tarball = _build_tarball(_MANIFEST_WITH_FEATURES)
    pack = await upload_pack(
        db_session,
        storage=storage,
        username="alice",
        origin_filename="vendor-featured-1.0.0.tar.gz",
        data=tarball,
    )
    await db_session.flush()

    release = pack.releases[0]
    feature_rows = (
        (await db_session.execute(select(DriverPackFeature).where(DriverPackFeature.pack_release_id == release.id)))
        .scalars()
        .all()
    )

    feature_ids = {f.manifest_feature_id for f in feature_rows}
    assert feature_ids == {"remote_debug", "screenshot_service"}

    remote_debug = next(f for f in feature_rows if f.manifest_feature_id == "remote_debug")
    assert remote_debug.data["display_name"] == "Remote Debug"
    assert remote_debug.data["help_url"] == "https://example.com/docs/remote-debug"

    screenshot = next(f for f in feature_rows if f.manifest_feature_id == "screenshot_service")
    assert screenshot.data["display_name"] == "Screenshot Service"


@pytest.mark.asyncio
async def test_upload_no_features_block_creates_no_feature_rows(db_session: AsyncSession, tmp_path: Path) -> None:
    """A manifest without a features block should not create any DriverPackFeature rows."""
    manifest_no_features = """\
schema_version: 1
id: vendor-plain
release: 1.0.0
display_name: Vendor Plain
appium_server:
  source: npm
  package: appium
  version: ">=2.5,<3"
  recommended: 2.19.0
appium_driver:
  source: npm
  package: appium-vendor-plain-driver
  version: ">=0,<1"
  recommended: 0.1.0
platforms:
  - id: vendor_p
    display_name: Vendor Platform
    automation_name: VendorAutomation
    appium_platform_name: Vendor
    device_types: [real_device]
    connection_types: [network]
    grid_slots: [native]
    capabilities: { stereotype: {}, session_required: [] }
    identity: { scheme: vendor_uid, scope: global }
"""
    storage = PackStorageService(root=tmp_path)
    tarball = _build_tarball(manifest_no_features)
    pack = await upload_pack(
        db_session,
        storage=storage,
        username="alice",
        origin_filename="vendor-plain-1.0.0.tar.gz",
        data=tarball,
    )
    await db_session.flush()

    release = pack.releases[0]
    feature_rows = (
        (await db_session.execute(select(DriverPackFeature).where(DriverPackFeature.pack_release_id == release.id)))
        .scalars()
        .all()
    )

    assert feature_rows == []
