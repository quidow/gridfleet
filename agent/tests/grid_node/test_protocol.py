from __future__ import annotations

import json
from pathlib import Path

from agent_app.grid_node.protocol import EventType, Slot, Stereotype, build_slots, event_envelope
from tests.grid_node.fixtures._substitute import substitute_placeholders

FIXTURES = Path(__file__).parent / "fixtures" / "decoded"


def test_known_event_types_match_captured_fixtures() -> None:
    assert {event.value for event in EventType} == {
        "node-added",
        "node-heartbeat",
        "session-created",
        "session-closed",
        "node-drain-started",
        "node-drain-complete",
        "node-removed",
    }


def test_event_envelope_shape_matches_captured_node_added() -> None:
    golden = substitute_placeholders(json.loads((FIXTURES / "01_node_bringup" / "node_added.json").read_text()))
    envelope = event_envelope(EventType.NODE_ADDED, golden["data"])
    assert envelope == golden


def test_build_slots_emits_android_native_and_chrome_slots() -> None:
    slots = build_slots(
        base_caps={"platformName": "Android", "appium:platform": "android_mobile"},
        grid_slots=["native", "chrome"],
    )
    assert [slot.stereotype.caps.get("browserName") for slot in slots] == [None, "chrome"]
    assert all(slot.stereotype.caps["platformName"] == "Android" for slot in slots)


def test_slot_round_trip_dict() -> None:
    slot = Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "iOS"}), state="AVAILABLE")
    assert Slot.from_dict(slot.to_dict()) == slot
