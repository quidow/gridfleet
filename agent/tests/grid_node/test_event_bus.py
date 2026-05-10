from __future__ import annotations

from agent_app.grid_node.event_bus import decode_event_frames, encode_event_frames
from agent_app.grid_node.protocol import EventType, event_envelope


def test_event_frames_round_trip_json_envelope() -> None:
    event = event_envelope(EventType.NODE_STATUS, {"id": "node-1"})
    frames = encode_event_frames(event)
    assert decode_event_frames(frames) == event
