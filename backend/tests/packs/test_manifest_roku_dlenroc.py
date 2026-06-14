from pathlib import Path

import pytest

from app.packs.manifest import Manifest, load_manifest_yaml

_MANIFEST_PATH = Path(__file__).resolve().parent / "fixtures" / "manifests" / "appium-roku-dlenroc.yaml"


@pytest.fixture()
def manifest() -> Manifest:
    return load_manifest_yaml(_MANIFEST_PATH.read_text())


def test_parses_without_error(manifest: Manifest) -> None:
    assert manifest.id == "appium-roku-dlenroc"


def test_single_platform_roku_network(manifest: Manifest) -> None:
    assert len(manifest.platforms) == 1
    plat = manifest.platforms[0]
    assert plat.id == "roku_network"
    assert plat.identity.scheme == "roku_serial"
    assert plat.identity.scope == "global"


def test_github_driver_source(manifest: Manifest) -> None:
    assert manifest.appium_driver.source == "github"
    assert manifest.appium_driver.github_repo == ("dlenroc/appium-roku-driver#b34f49a8652d70f669cac7ec86805ed4378aaff8")
    assert manifest.appium_driver.package == "@dlenroc/appium-roku-driver"
    assert manifest.appium_driver.recommended == "0.13.3"
    assert "0.13.1" in manifest.appium_driver.known_bad


def test_roku_password_field(manifest: Manifest) -> None:
    plat = manifest.platforms[0]
    assert len(plat.device_fields_schema) == 1
    field = plat.device_fields_schema[0]
    assert field.id == "roku_password"
    assert field.sensitive is True
    assert field.required_for_session is True
    assert field.capability_name == "appium:password"
