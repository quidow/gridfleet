import pytest
from pydantic import ValidationError

from app.devices.schemas.portability import (
    ExportBundle,
    ExportedDevice,
    ImportPreview,
    ImportRowStatus,
    OriginalHost,
)


def test_exported_device_strict_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        ExportedDevice.model_validate(
            {
                "pack_id": "appium-uiautomator2",
                "platform_id": "android",
                "identity_scheme": "serial",
                "identity_scope": "host",
                "identity_value": "R58",
                "name": "Pixel",
                "device_type": "real_device",
                "connection_type": "usb",
                "auto_manage": True,
                "tags": {},
                "device_config": {},
                "test_data": {},
                "original_host": {"hostname": "lab-04"},
                "unexpected": True,
            }
        )


def test_export_bundle_schema_version_required() -> None:
    with pytest.raises(ValidationError):
        ExportBundle.model_validate({"exported_at": "2026-05-23T00:00:00Z", "devices": []})


def test_original_host_host_id_optional() -> None:
    host = OriginalHost.model_validate({"hostname": "lab-04"})
    assert host.host_id is None


def test_import_preview_schema_version_required() -> None:
    with pytest.raises(ValidationError):
        ImportPreview.model_validate(
            {
                "exported_at": "2026-05-23T00:00:00Z",
                "bundle_hash": "sha256:x",
                "available_hosts": [],
                "rows": [],
            }
        )


def test_exported_device_identity_scope_rejects_unknown_value() -> None:
    payload = {
        "pack_id": "appium-uiautomator2",
        "platform_id": "android",
        "identity_scheme": "serial",
        "identity_scope": "fleet",
        "identity_value": "R58",
        "name": "Pixel",
        "device_type": "real_device",
        "connection_type": "usb",
        "auto_manage": True,
        "tags": {},
        "device_config": {},
        "test_data": {},
        "original_host": {"hostname": "lab-04"},
    }
    with pytest.raises(ValidationError):
        ExportedDevice.model_validate(payload)


def test_import_row_status_enum_values() -> None:
    assert ImportRowStatus.VALID_NEW == "valid_new"
    assert ImportRowStatus.CONFLICT_SKIP == "conflict_skip"
    assert ImportRowStatus.DUPLICATE_IN_BUNDLE == "duplicate_in_bundle"
    assert ImportRowStatus.INVALID == "invalid"
    assert {m.value for m in ImportRowStatus} == {
        "valid_new",
        "conflict_skip",
        "duplicate_in_bundle",
        "invalid",
    }
