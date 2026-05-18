"""Decoder for Selenium Grid 4 event-bus wire frames.

Frames arrive as ``[event-name, secret, event-id, data]`` per
``UnboundZmqEventBus$PollingRunnable``. This is the backend twin of
``agent_app/grid_node/event_bus.py``; any change to the wire format
must touch both files.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app.grid.event_bus import (
    DecodedEvent,
    decode_event_frames,
    parse_session_closed_id,
)


def _frames(event_type: str, payload: object) -> list[bytes]:
    return [
        event_type.encode("utf-8"),
        b'""',
        str(uuid4()).encode("ascii"),
        json.dumps(payload, sort_keys=True).encode("utf-8"),
    ]


def test_decode_session_created() -> None:
    payload = {"id": "session-1", "capabilities": {"platformName": "Android"}}
    decoded = decode_event_frames(_frames("session-created", payload))
    assert decoded == DecodedEvent(type="session-created", data=payload)


def test_decode_session_closed_legacy_string_payload() -> None:
    decoded = decode_event_frames(_frames("session-closed", "session-1"))
    assert decoded.type == "session-closed"
    assert decoded.data == "session-1"


def test_decode_rejects_too_few_frames() -> None:
    with pytest.raises(ValueError, match="expected 4"):
        decode_event_frames([b"session-created", b'""', b"id"])


def test_decode_rejects_empty_event_type() -> None:
    frames = [b"", b'""', b"id", b'"x"']
    with pytest.raises(ValueError, match="missing event type"):
        decode_event_frames(frames)


def test_decode_rejects_malformed_json() -> None:
    frames = [b"session-created", b'""', b"id", b"not-json"]
    with pytest.raises(ValueError, match="malformed grid event-bus frames"):
        decode_event_frames(frames)


def test_parse_session_closed_id_legacy_string() -> None:
    """Selenium <4.44 emits the session id as a bare JSON string."""
    assert parse_session_closed_id("session-1") == "session-1"


def test_parse_session_closed_id_object_payload() -> None:
    """Selenium 4.44+ wraps the id in an object. Lenient parser accepts both."""
    assert parse_session_closed_id({"id": "session-1"}) == "session-1"


def test_parse_session_closed_id_unknown_shape_returns_none() -> None:
    assert parse_session_closed_id({"foo": "bar"}) is None
    assert parse_session_closed_id(None) is None
    assert parse_session_closed_id(42) is None
