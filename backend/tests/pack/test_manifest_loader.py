import textwrap

import pytest

from app.packs.manifest import Manifest, ManifestValidationError, load_manifest_yaml


def _valid_yaml() -> str:
    return textwrap.dedent(
        """
        schema_version: 1
        id: appium-uiautomator2
        release: 2026.04.0
        display_name: Appium UiAutomator2
        maintainer: gridfleet-team
        license: Apache-2.0
        requires:
          gridfleet: ">=1.7"
          host_os: [linux, macos]
        appium_server:
          source: npm
          package: appium
          version: ">=2.5,<3"
          recommended: "2.11.5"
          known_bad: []
        appium_driver:
          source: npm
          package: appium-uiautomator2-driver
          version: ">=3,<5"
          recommended: "3.6.0"
          known_bad: []
        platforms:
          - id: android_mobile
            display_name: "Android (real device)"
            automation_name: UiAutomator2
            appium_platform_name: Android
            device_types: [real_device]
            connection_types: [usb, network]
            grid_slots: [native]
            capabilities:
              stereotype: { "appium:platformName": "Android" }
              session_required: []
            identity:
              scheme: android_serial
              scope: host
        """
    ).strip()


def test_load_valid_manifest() -> None:
    manifest = load_manifest_yaml(_valid_yaml())
    assert isinstance(manifest, Manifest)
    assert manifest.id == "appium-uiautomator2"
    assert manifest.release == "2026.04.0"
    assert manifest.requires.host_os == ["linux", "macos"]
    assert manifest.platforms[0].identity.scheme == "android_serial"


def test_discovery_block_rejected() -> None:
    yaml_text = _valid_yaml().replace(
        "identity:\n      scheme: android_serial\n      scope: host",
        "discovery:\n      kind: totally_made_up\n    identity:\n      scheme: android_serial\n      scope: host",
    )
    with pytest.raises(ManifestValidationError, match=r"discovery"):
        load_manifest_yaml(yaml_text)


def test_recommended_driver_version_must_satisfy_range() -> None:
    yaml_text = _valid_yaml().replace('recommended: "3.6.0"', 'recommended: "5.0.0"')
    with pytest.raises(ManifestValidationError, match=r"recommended version.*does not satisfy"):
        load_manifest_yaml(yaml_text)


def test_recommended_server_version_must_satisfy_range() -> None:
    yaml_text = _valid_yaml().replace('recommended: "2.11.5"', 'recommended: "3.0.0"')
    with pytest.raises(ManifestValidationError, match=r"recommended version.*does not satisfy"):
        load_manifest_yaml(yaml_text)


def test_manifest_accepts_display_icon_kind() -> None:
    yaml_text = _valid_yaml().replace(
        "identity:\n      scheme: android_serial\n      scope: host",
        "identity:\n      scheme: android_serial\n      scope: host\n    display:\n      icon_kind: mobile",
    )
    manifest = load_manifest_yaml(yaml_text)
    assert manifest.platforms[0].display.icon_kind == "mobile"


def test_manifest_rejects_unknown_icon_kind() -> None:
    yaml_text = _valid_yaml().replace(
        "identity:\n      scheme: android_serial\n      scope: host",
        "identity:\n      scheme: android_serial\n      scope: host\n    display:\n      icon_kind: bogus",
    )
    with pytest.raises(ManifestValidationError, match=r"icon_kind"):
        load_manifest_yaml(yaml_text)


def test_manifest_accepts_default_capabilities_with_interpolation() -> None:
    replacement = (
        "identity:\n      scheme: android_serial\n      scope: host\n"
        '    default_capabilities:\n      "appium:wdaBaseUrl": '
        '"http://{device.ip_address}"\n      "appium:usePreinstalledWDA": true'
    )
    yaml_text = _valid_yaml().replace(
        "identity:\n      scheme: android_serial\n      scope: host",
        replacement,
    )
    manifest = load_manifest_yaml(yaml_text)
    caps = manifest.platforms[0].default_capabilities
    assert caps["appium:wdaBaseUrl"] == "http://{device.ip_address}"


def test_manifest_rejects_default_capability_with_unknown_template_var() -> None:
    replacement = (
        "identity:\n      scheme: android_serial\n      scope: host\n"
        '    default_capabilities:\n      "appium:wdaBaseUrl": '
        '"http://{device.bogus}"'
    )
    yaml_text = _valid_yaml().replace(
        "identity:\n      scheme: android_serial\n      scope: host",
        replacement,
    )
    with pytest.raises(ManifestValidationError, match=r"template variable"):
        load_manifest_yaml(yaml_text)


def test_manifest_accepts_insecure_features() -> None:
    yaml_text = _valid_yaml() + "\ninsecure_features:\n  - uiautomator2:chromedriver_autodownload\n"
    manifest = load_manifest_yaml(yaml_text)
    assert "uiautomator2:chromedriver_autodownload" in manifest.insecure_features


def test_manifest_accepts_workarounds_block() -> None:
    yaml_text = _valid_yaml() + (
        "\nworkarounds:\n"
        "  - id: wk1\n"
        "    applies_when:\n"
        "      platform_ids: [android_mobile]\n"
        "      device_types: [real_device]\n"
        "    env:\n"
        '      FOO: "1"\n'
    )
    manifest = load_manifest_yaml(yaml_text)
    assert manifest.workarounds[0].id == "wk1"
    assert manifest.workarounds[0].env == {"FOO": "1"}


def test_manifest_rejects_workaround_without_id() -> None:
    yaml_text = _valid_yaml() + '\nworkarounds:\n  - env:\n      FOO: "1"\n'
    with pytest.raises(ManifestValidationError):
        load_manifest_yaml(yaml_text)


def test_manifest_accepts_device_fields_schema_default() -> None:
    replacement = (
        "identity:\n      scheme: android_serial\n      scope: host\n"
        "    device_fields_schema:\n      - id: f1\n        label: F1\n"
        "        type: bool\n        default: true"
    )
    yaml_text = _valid_yaml().replace(
        "identity:\n      scheme: android_serial\n      scope: host",
        replacement,
    )
    manifest = load_manifest_yaml(yaml_text)
    assert manifest.platforms[0].device_fields_schema[0].default is True
