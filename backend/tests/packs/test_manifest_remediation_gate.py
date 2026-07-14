import textwrap

import pytest

from app.packs.manifest import ManifestValidationError, load_manifest_yaml


def _manifest_yaml(action: str, *, device_type_override: bool = False) -> str:
    manifest = textwrap.dedent(
        """
        schema_version: 1
        id: synthetic-remediation-gate
        release: 1.0.0
        display_name: Synthetic remediation gate
        appium_server:
          source: npm
          package: appium
          version: ">=2,<3"
        appium_driver:
          source: npm
          package: synthetic-driver
          version: ">=1,<2"
        platforms:
          - id: synthetic_platform
            display_name: Synthetic platform
            automation_name: Synthetic
            appium_platform_name: Synthetic
            device_types: [emulator]
            connection_types: [virtual]
            capabilities:
              stereotype: {}
              session_required: []
            identity:
              scheme: synthetic_id
              scope: host
        """
    ).strip()
    action_yaml = textwrap.indent(action, " " * (10 if device_type_override else 6))
    if device_type_override:
        return f"{manifest}\n    device_type_overrides:\n      emulator:\n        lifecycle_actions:\n{action_yaml}\n"
    return f"{manifest}\n    lifecycle_actions:\n{action_yaml}\n"


@pytest.mark.parametrize("action_id", ["reconnect", "release_forwarded_ports"])
def test_load_manifest_accepts_repeat_safe_remediation_action(action_id: str) -> None:
    manifest = load_manifest_yaml(_manifest_yaml(f"- id: {action_id}\n  remediation: true"))

    assert manifest.platforms[0].lifecycle_actions[0].remediation is True


def test_load_manifest_rejects_non_repeat_safe_remediation_action() -> None:
    with pytest.raises(ManifestValidationError, match="not repeat-safe") as exc_info:
        load_manifest_yaml(_manifest_yaml("- id: boot\n  remediation: true"))

    message = str(exc_info.value)
    assert "synthetic_platform" in message
    assert "boot" in message
    assert "reconnect" in message
    assert "release_forwarded_ports" in message


def test_load_manifest_accepts_unmarked_operator_action() -> None:
    manifest = load_manifest_yaml(_manifest_yaml("- id: boot"))

    assert manifest.platforms[0].lifecycle_actions[0].remediation is False


def test_load_manifest_rejects_non_repeat_safe_remediation_action_in_device_type_override() -> None:
    with pytest.raises(ManifestValidationError, match="not repeat-safe") as exc_info:
        load_manifest_yaml(
            _manifest_yaml(
                "- id: shutdown\n  remediation: true",
                device_type_override=True,
            )
        )

    message = str(exc_info.value)
    assert "synthetic_platform" in message
    assert "shutdown" in message
    assert "reconnect" in message
    assert "release_forwarded_ports" in message
