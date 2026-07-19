from app.devices.schemas.device import DeviceRead


def test_device_read_exposes_pack_labels() -> None:
    fields = set(DeviceRead.model_fields)

    assert "pack_id" in fields
    assert "platform_id" in fields
    assert "platform_label" in fields
    assert "identity_scheme" in fields
    assert "identity_scope" in fields
    assert "platform" not in fields
    assert "identity_kind" not in fields
