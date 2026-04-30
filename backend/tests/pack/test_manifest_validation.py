import textwrap
from pathlib import Path

import pytest

from app.pack.manifest import ManifestValidationError, load_manifest_yaml


def _base_yaml() -> str:
    return textwrap.dedent(
        """
        schema_version: 1
        id: local/test
        release: 2026.04.0
        display_name: Test
        appium_server:
          {source: npm, package: appium, version: ">=2,<3", recommended: "2.11.5", known_bad: []}
        appium_driver:
          {source: npm, package: appium-test-driver, version: ">=1,<2", recommended: "1.0.0", known_bad: []}
        platforms:
          - id: test_network
            display_name: Test Network
            automation_name: Test
            appium_platform_name: TestOS
            device_types: [real_device]
            connection_types: [network]
            grid_slots: [native]
            capabilities: {stereotype: {"appium:platformName": "TestOS"}}
            identity: {scheme: test_id, scope: host}
        """
    ).strip()


# After dedent the platform fields have 4-space indent at the top level;
# "identity" line is at 4-space inside the list item (12 spaces total in the
# raw triple-quoted string, but after dedent the common prefix "        " is
# removed leaving 4 spaces).
_IDENTITY_LINE = "    identity: {scheme: test_id, scope: host}"


def test_platform_accepts_connection_behavior_metadata() -> None:
    yaml_text = _base_yaml().replace(
        _IDENTITY_LINE,
        (
            _IDENTITY_LINE + "\n"
            "    connection_behavior:\n"
            "      default_device_type: real_device\n"
            "      default_connection_type: network\n"
            "      requires_ip_address: true\n"
            "      requires_connection_target: true\n"
            "      allow_transport_identity_until_host_resolution: false\n"
        ),
    )
    manifest = load_manifest_yaml(yaml_text)
    platform = manifest.platforms[0]
    assert platform.connection_behavior.default_connection_type == "network"
    assert platform.connection_behavior.requires_ip_address is True


def test_platform_connection_behavior_defaults() -> None:
    """Platform without connection_behavior gets sensible defaults."""
    manifest = load_manifest_yaml(_base_yaml())
    platform = manifest.platforms[0]
    assert platform.connection_behavior.default_device_type is None
    assert platform.connection_behavior.default_connection_type is None
    assert platform.connection_behavior.requires_ip_address is False
    assert platform.connection_behavior.requires_connection_target is True
    assert platform.connection_behavior.allow_transport_identity_until_host_resolution is False
    assert platform.connection_behavior.host_resolution_action is None


def test_platform_accepts_health_check_labels() -> None:
    yaml_text = _base_yaml().replace(
        _IDENTITY_LINE,
        (
            _IDENTITY_LINE + "\n"
            "    health_checks:\n"
            "      - id: adb_connected\n"
            "        label: ADB Connected\n"
            "      - id: boot_completed\n"
            "        label: Boot Completed\n"
        ),
    )

    manifest = load_manifest_yaml(yaml_text)

    assert [check.model_dump() for check in manifest.platforms[0].health_checks] == [
        {"id": "adb_connected", "label": "ADB Connected"},
        {"id": "boot_completed", "label": "Boot Completed"},
    ]


def test_platform_health_check_rejects_empty_id() -> None:
    yaml_text = _base_yaml().replace(
        _IDENTITY_LINE,
        (_IDENTITY_LINE + "\n    health_checks:\n      - id: ''\n        label: ADB Connected\n"),
    )

    with pytest.raises(ManifestValidationError, match="health check id must not be empty"):
        load_manifest_yaml(yaml_text)


def test_platform_health_check_rejects_empty_label() -> None:
    yaml_text = _base_yaml().replace(
        _IDENTITY_LINE,
        (_IDENTITY_LINE + "\n    health_checks:\n      - id: adb_connected\n        label: ' '\n"),
    )

    with pytest.raises(ManifestValidationError, match="health check label must not be empty"):
        load_manifest_yaml(yaml_text)


def test_platform_connection_behavior_rejects_unknown_field() -> None:
    yaml_text = _base_yaml().replace(
        _IDENTITY_LINE,
        (_IDENTITY_LINE + "\n    connection_behavior:\n      bogus_field: true\n"),
    )
    with pytest.raises(ManifestValidationError, match=r"bogus_field"):
        load_manifest_yaml(yaml_text)


def test_platform_connection_behavior_rejects_invalid_device_type() -> None:
    yaml_text = _base_yaml().replace(
        _IDENTITY_LINE,
        (_IDENTITY_LINE + "\n    connection_behavior:\n      default_device_type: flying_device\n"),
    )
    with pytest.raises(ManifestValidationError):
        load_manifest_yaml(yaml_text)


def test_platform_connection_behavior_rejects_invalid_connection_type() -> None:
    yaml_text = _base_yaml().replace(
        _IDENTITY_LINE,
        (_IDENTITY_LINE + "\n    connection_behavior:\n      default_connection_type: fiber\n"),
    )
    with pytest.raises(ManifestValidationError):
        load_manifest_yaml(yaml_text)


def test_platform_connection_behavior_host_resolution_action() -> None:
    yaml_text = _base_yaml().replace(
        _IDENTITY_LINE,
        (
            _IDENTITY_LINE + "\n"
            "    connection_behavior:\n"
            "      allow_transport_identity_until_host_resolution: true\n"
            "      host_resolution_action: resolve\n"
        ),
    )
    manifest = load_manifest_yaml(yaml_text)
    cb = manifest.platforms[0].connection_behavior
    assert cb.allow_transport_identity_until_host_resolution is True
    assert cb.host_resolution_action == "resolve"


def test_manifest_rejects_legacy_discovery_block() -> None:
    yaml_text = _base_yaml().replace(
        _IDENTITY_LINE,
        "    discovery: {kind: network_endpoint}\n" + _IDENTITY_LINE,
    )
    with pytest.raises(ManifestValidationError, match=r"discovery"):
        load_manifest_yaml(yaml_text)


def test_curated_manifests_pin_appium_and_driver_versions() -> None:
    curated_dir = Path(__file__).resolve().parents[3] / "driver-packs" / "curated"
    expected = {
        "appium-roku-dlenroc": ("==3.3.1", "3.3.1", "==0.13.3", "0.13.3"),
        "appium-uiautomator2": ("==3.3.1", "3.3.1", "==5.0.6", "5.0.6"),
        "appium-xcuitest": ("==3.3.1", "3.3.1", "==10.33.0", "10.33.0"),
    }

    for pack_id, pins in expected.items():
        manifest = load_manifest_yaml((curated_dir / pack_id / "manifest.yaml").read_text())
        server_version, server_recommended, driver_version, driver_recommended = pins
        assert manifest.appium_server.version == server_version
        assert manifest.appium_server.recommended == server_recommended
        assert manifest.appium_driver.version == driver_version
        assert manifest.appium_driver.recommended == driver_recommended
