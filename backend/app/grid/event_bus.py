"""Backend subscriber for the Selenium Grid hub event bus.

This module is the backend twin of ``agent_app/grid_node/event_bus.py``.
Both decode the same four-frame wire format produced by Selenium's
``UnboundZmqEventBus``: ``[event-name, secret, event-id, data]``. Keep
the decoders in sync — any wire-format change must touch both files.

The subscriber class lives in this module too (added in Task 7); only
the decoder and session-closed payload parser are exposed here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DecodedEvent:
    type: str
    data: Any


def decode_event_frames(frames: list[bytes]) -> DecodedEvent:
    """Decode Selenium 4 event-bus frames into a ``DecodedEvent``.

    Raises ``ValueError`` on any structural problem. The subscriber loop
    catches the error, counts it (``grid_event_bus_decode_failures_total``),
    and continues — a malformed frame must not break the subscriber.
    """
    if len(frames) < 4:
        raise ValueError(f"expected 4 grid event-bus frames, got {len(frames)}")
    try:
        event_type = frames[0].decode("utf-8")
        data = json.loads(frames[3].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed grid event-bus frames") from exc
    if not event_type:
        raise ValueError("missing event type frame")
    return DecodedEvent(type=event_type, data=data)


def parse_session_closed_id(payload: Any) -> str | None:  # noqa: ANN401
    """Extract the session id from a ``session-closed`` payload.

    Lenient by design. Selenium <4.44 emits the bare session id as a
    JSON string; 4.44+ wraps it (#17343). Either is accepted; anything
    else returns ``None`` so the subscriber falls back to the
    reconciler instead of crashing.
    """
    if isinstance(payload, str) and payload:
        return payload
    if isinstance(payload, dict):
        candidate = payload.get("id")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None
