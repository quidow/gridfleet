from __future__ import annotations

import io
import tarfile
from typing import TYPE_CHECKING

import pytest
import yaml

import app.services.pack_template_service as svc

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(autouse=True)
def clear_cache() -> Generator[None, None, None]:
    svc._TEMPLATE_CACHE = None
    yield
    svc._TEMPLATE_CACHE = None


def test_list_templates_returns_descriptors() -> None:
    descriptors = svc.list_templates()
    ids = {descriptor.id for descriptor in descriptors}
    assert "appium-uiautomator2-android-real" in ids
    assert "appium-xcuitest-ios-real" in ids
    for descriptor in descriptors:
        assert descriptor.display_name
        assert descriptor.target_driver_summary
        assert descriptor.source_pack_id
        assert descriptor.prerequisite_host_tools


def test_load_template_keeps_manifest_dict_without_metadata() -> None:
    template = svc.load_template("appium-uiautomator2-android-real")
    assert template.descriptor.id == "appium-uiautomator2-android-real"
    assert template.descriptor.source_pack_id == "appium-uiautomator2"
    assert template.manifest_dict["id"] == "local/uiautomator2-android-real"
    assert "template_metadata" not in template.manifest_dict
    assert "template_metadata" in template.raw_yaml


def test_build_tarball_from_template_rewrites_manifest() -> None:
    template = svc.load_template("appium-uiautomator2-android-real")
    data = svc.build_tarball_from_template(
        template,
        pack_id="vendor/android-real",
        release="1.2.3",
        display_name="Vendor Android",
    )

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        manifest_file = tar.extractfile("manifest.yaml")
        assert manifest_file is not None
        manifest = yaml.safe_load(manifest_file.read())

    assert manifest["id"] == "vendor/android-real"
    assert manifest["release"] == "1.2.3"
    assert manifest["display_name"] == "Vendor Android"
    assert manifest["template_id"] == "appium-uiautomator2-android-real"
    assert manifest["derived_from"] == {"pack_id": "appium-uiautomator2", "release": "1.2.3"}
    assert "origin" not in manifest


def test_unknown_template_raises_lookup_error() -> None:
    with pytest.raises(LookupError, match="not found"):
        svc.load_template("nonexistent-template-xyz")
