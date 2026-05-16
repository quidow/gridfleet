"""Unit tests for ``app.grid.slot_parser``.

The parser is the single source of truth for turning a Selenium Grid
``/status`` payload into trackable session metadata. Both the
``session_sync_loop`` and the ``/api/grid/status`` counter consume it, so a
regression here would silently desync the Sessions table, the device state
machine, and the dashboard "Sessions" tile.
"""

from __future__ import annotations

import uuid

from app.grid.slot_parser import iter_slot_sessions, parse_slot_session
from app.sessions.probe_constants import PROBE_TEST_NAME


def _slot(session: dict | None) -> dict:
    return {"session": session}


def test_returns_none_for_empty_slot() -> None:
    assert parse_slot_session(_slot(None)) is None
    assert parse_slot_session(_slot({})) is None
    assert parse_slot_session({}) is None
    assert parse_slot_session("not-a-dict") is None  # type: ignore[arg-type]


def test_returns_none_for_reserved_sentinel() -> None:
    assert parse_slot_session(_slot({"sessionId": "reserved"})) is None


def test_returns_none_when_session_id_missing() -> None:
    assert parse_slot_session(_slot({"capabilities": {}})) is None


def test_reads_identity_from_stereotype_not_capabilities() -> None:
    """Real Selenium 4 hub shape: capabilities lack ``appium:`` prefix and
    omit the gridfleet device id; both fields live on stereotype."""
    device_id = str(uuid.uuid4())
    parsed = parse_slot_session(
        _slot(
            {
                "sessionId": "sess-1",
                "capabilities": {
                    "platformName": "ANDROID",
                    "udid": "192.168.1.254:5555",
                    "deviceName": "192.168.1.254:5555",
                },
                "stereotype": {
                    "appium:gridfleet:deviceId": device_id,
                    "appium:udid": "192.168.1.254:5555",
                    "platformName": "ANDROID",
                },
            }
        )
    )
    assert parsed is not None
    assert parsed.session_id == "sess-1"
    assert parsed.device_id == uuid.UUID(device_id)
    assert parsed.connection_target == "192.168.1.254:5555"
    assert parsed.is_probe is False


def test_falls_back_to_slot_level_stereotype() -> None:
    """Some older hub versions emit stereotype on the slot, not the session."""
    device_id = str(uuid.uuid4())
    parsed = parse_slot_session(
        {
            "stereotype": {
                "appium:gridfleet:deviceId": device_id,
                "appium:udid": "fallback-target",
            },
            "session": {
                "sessionId": "sess-2",
                "capabilities": {"platformName": "ANDROID"},
            },
        }
    )
    assert parsed is not None
    assert parsed.device_id == uuid.UUID(device_id)
    assert parsed.connection_target == "fallback-target"


def test_capability_side_fallback_for_connection_target() -> None:
    """If neither stereotype side advertises a udid, accept what
    capabilities exposes (bare ``udid`` for stripped W3C, or prefixed for
    non-Appium drivers)."""
    parsed = parse_slot_session(
        _slot(
            {
                "sessionId": "sess-3",
                "capabilities": {"udid": "bare-udid"},
            }
        )
    )
    assert parsed is not None
    assert parsed.connection_target == "bare-udid"
    assert parsed.device_id is None


def test_probe_flag_via_explicit_marker() -> None:
    parsed = parse_slot_session(
        _slot(
            {
                "sessionId": "probe-1",
                "capabilities": {
                    "gridfleet:probeSession": True,
                    "gridfleet:testName": PROBE_TEST_NAME,
                },
            }
        )
    )
    assert parsed is not None
    assert parsed.is_probe is True


def test_probe_flag_via_test_name_alone() -> None:
    parsed = parse_slot_session(
        _slot(
            {
                "sessionId": "probe-2",
                "capabilities": {"gridfleet:testName": PROBE_TEST_NAME},
            }
        )
    )
    assert parsed is not None
    assert parsed.is_probe is True


def test_invalid_device_id_uuid_is_dropped() -> None:
    parsed = parse_slot_session(
        _slot(
            {
                "sessionId": "sess-bad-uuid",
                "capabilities": {"udid": "ct"},
                "stereotype": {"appium:gridfleet:deviceId": "not-a-uuid"},
            }
        )
    )
    assert parsed is not None
    assert parsed.device_id is None
    assert parsed.connection_target == "ct"


def test_iter_walks_all_nodes_and_slots() -> None:
    device_id = str(uuid.uuid4())
    payload = {
        "value": {
            "nodes": [
                {
                    "slots": [
                        _slot(
                            {
                                "sessionId": "real",
                                "capabilities": {"udid": "ct-a"},
                                "stereotype": {"appium:gridfleet:deviceId": device_id},
                            }
                        ),
                        _slot(None),
                    ]
                },
                {
                    "slots": [
                        _slot(
                            {
                                "sessionId": "probe",
                                "capabilities": {"gridfleet:probeSession": True},
                            }
                        )
                    ]
                },
            ]
        }
    }
    parsed = iter_slot_sessions(payload)
    assert [p.session_id for p in parsed] == ["real", "probe"]
    assert parsed[0].is_probe is False
    assert parsed[0].device_id == uuid.UUID(device_id)
    assert parsed[1].is_probe is True


def test_iter_tolerates_malformed_payload() -> None:
    assert iter_slot_sessions(None) == []
    assert iter_slot_sessions("nope") == []
    assert iter_slot_sessions({"value": "wrong"}) == []
    assert iter_slot_sessions({"value": {"nodes": "wrong"}}) == []
    assert iter_slot_sessions({"value": {"nodes": [{"slots": "wrong"}]}}) == []
