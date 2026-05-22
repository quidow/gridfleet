import pytest
from pydantic import ValidationError

from app.devices.schemas.portability import (
    ExportBundle,
    ExportedDevice,
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
