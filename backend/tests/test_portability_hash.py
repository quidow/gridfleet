from app.devices.schemas.portability import ExportBundle
from app.devices.services.portability_hash import canonical_bundle_json, compute_bundle_hash


def _bundle_dict() -> dict:
    return {
        "schema_version": 1,
        "exported_at": "2026-05-23T00:00:00+00:00",
        "source_instance": "alpha",
        "devices": [
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
            }
        ],
    }


def test_hash_is_stable_across_key_order() -> None:
    a = ExportBundle.model_validate(_bundle_dict())
    rearranged = _bundle_dict()
    rearranged["devices"] = [{k: v for k, v in reversed(list(rearranged["devices"][0].items()))}]
    b = ExportBundle.model_validate(rearranged)
    assert compute_bundle_hash(a) == compute_bundle_hash(b)
    assert compute_bundle_hash(a).startswith("sha256:")


def test_canonical_json_sorts_keys_and_strips_whitespace() -> None:
    bundle = ExportBundle.model_validate(_bundle_dict())
    canon = canonical_bundle_json(bundle)
    assert b" " not in canon.replace(b'", "', b"XXX")  # no extra spaces between key:value pairs
    # sorted keys: "devices" should appear before "exported_at" before "schema_version"...
    assert canon.index(b'"devices"') < canon.index(b'"exported_at"') < canon.index(b'"schema_version"')


def test_hash_differs_on_data_change() -> None:
    a = ExportBundle.model_validate(_bundle_dict())
    modified = _bundle_dict()
    modified["devices"][0]["name"] = "Pixel 7"
    b = ExportBundle.model_validate(modified)
    assert compute_bundle_hash(a) != compute_bundle_hash(b)
