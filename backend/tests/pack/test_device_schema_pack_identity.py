import uuid

import pytest
from pydantic import ValidationError

from app.devices.schemas.device import BulkTagsUpdate, DevicePatch, DeviceRead


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
