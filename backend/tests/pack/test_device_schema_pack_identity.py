import uuid

import pytest
from pydantic import ValidationError

from app.devices.schemas.device import BulkTagsUpdate, DeviceCreate, DevicePatch, DeviceRead


def test_device_create_accepts_pack_identity_fields() -> None:
    payload = DeviceCreate.model_validate(
        {
            "name": "Pixel 8",
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
            "identity_scheme": "android_serial",
            "identity_scope": "host",
            "identity_value": "serial-1",
            "connection_target": "serial-1",
            "os_version": "14",
            "host_id": "11111111-1111-1111-1111-111111111111",
            "device_type": "real_device",
            "connection_type": "usb",
        }
    )

    assert payload.pack_id == "appium-uiautomator2"
    assert payload.platform_id == "android_mobile"
    assert payload.identity_scheme == "android_serial"
    assert payload.identity_scope == "host"


def test_device_read_exposes_pack_labels() -> None:
    fields = set(DeviceRead.model_fields)

    assert "pack_id" in fields
    assert "platform_id" in fields
    assert "platform_label" in fields
    assert "identity_scheme" in fields
    assert "identity_scope" in fields
    assert "platform" not in fields
    assert "identity_kind" not in fields


@pytest.mark.parametrize(
    ("schema_cls", "payload"),
    [
        (DevicePatch, {"tags": {"priority": 1}}),
        (
            BulkTagsUpdate,
            {"device_ids": [uuid.uuid4()], "tags": {"priority": 1}},
        ),
    ],
)
def test_device_tag_schemas_reject_non_string_values(
    schema_cls: type[DevicePatch | BulkTagsUpdate],
    payload: object,
) -> None:
    with pytest.raises(ValidationError):
        schema_cls.model_validate(payload)
