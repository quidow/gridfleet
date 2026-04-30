from app.pack.manifest import load_manifest_yaml

MINIMAL_MANIFEST = """\
schema_version: 1
id: test-pack
release: "1.0.0"
display_name: Test Pack
appium_server:
  source: npm
  package: appium
  version: ">=2.5,<3"
  recommended: "2.11.5"
appium_driver:
  source: npm
  package: appium-uiautomator2-driver
  version: ">=3,<5"
  recommended: "3.6.0"
platforms:
  - id: test_platform
    display_name: Test Platform
    automation_name: UiAutomator2
    appium_platform_name: Android
    device_types: [real_device]
    connection_types: [usb]
    grid_slots: [native]
    capabilities:
      stereotype:
        "appium:platformName": "Android"
    identity:
      scheme: android_serial
      scope: host
"""


def test_manifest_loads_without_origin() -> None:
    manifest = load_manifest_yaml(MINIMAL_MANIFEST)
    assert manifest.id == "test-pack"
    assert not hasattr(manifest, "origin")


def test_manifest_ignores_origin_if_present() -> None:
    with_origin = MINIMAL_MANIFEST + "origin: uploaded\n"
    manifest = load_manifest_yaml(with_origin)
    assert manifest.id == "test-pack"
    assert not hasattr(manifest, "origin")


def test_manifest_no_local_pack_id_restriction() -> None:
    yaml_text = MINIMAL_MANIFEST.replace("id: test-pack", "id: local/my-pack")
    manifest = load_manifest_yaml(yaml_text)
    assert manifest.id == "local/my-pack"
