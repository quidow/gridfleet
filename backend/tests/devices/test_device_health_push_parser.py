from __future__ import annotations

import uuid

from app.devices.schemas.device_health_push import parse_device_health_items


def test_parses_v7_list_section() -> None:
    did = uuid.uuid4()
    section = {
        "complete_gather": True,
        "devices": [
            {
                "device_id": str(did),
                "probe_status": "observed",
                "presence": "present",
                "health": {"healthy": True, "checks": []},
                "lifecycle_state": {"status": "observed", "value": "device"},
            },
            {
                "device_id": "not-a-uuid",
                "probe_status": "error",
                "presence": "unknown",
                "health": None,
                "lifecycle_state": {},
            },
        ],
    }
    parsed = parse_device_health_items(section)
    assert parsed.is_v7 is True
    assert parsed.complete_gather is True
    assert set(parsed.by_device_id) == {did}  # malformed device_id dropped
    item = parsed.by_device_id[did]
    assert item.presence == "present"
    assert item.probe_status == "observed"
    assert item.health == {"healthy": True, "checks": []}
    assert not hasattr(item, "lifecycle_state")


def test_legacy_dict_section_is_not_v7() -> None:
    section = {"devices": {"emulator-5554": {"healthy": False, "checks": []}}}
    parsed = parse_device_health_items(section)
    assert parsed.is_v7 is False
    assert parsed.complete_gather is False
    assert parsed.by_device_id == {}


def test_missing_devices_key_is_legacy_empty() -> None:
    assert parse_device_health_items({"reported_at": "x"}).is_v7 is False


def test_malformed_v7_evidence_fails_safe() -> None:
    did = uuid.uuid4()
    parsed = parse_device_health_items(
        {
            # Truthy non-booleans must not authorize absence assertions.
            "complete_gather": "false",
            "devices": [
                {
                    "device_id": str(did),
                    "probe_status": "unexpected",
                    "presence": "unexpected",
                    "health": {"healthy": False, "checks": []},
                    "lifecycle_state": {},
                }
            ],
        }
    )

    assert parsed.complete_gather is False
    assert parsed.by_device_id[did].probe_status == "error"
    assert parsed.by_device_id[did].presence == "unknown"
