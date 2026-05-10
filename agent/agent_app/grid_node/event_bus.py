from __future__ import annotations

import json
from typing import Any
from uuid import uuid4


def encode_event_frames(event: dict[str, Any]) -> list[bytes]:
    return [
        str(event["type"]).lower().encode("utf-8"),
        b'""',
        str(uuid4()).encode("ascii"),
        json.dumps(event, sort_keys=True).encode("utf-8"),
    ]


def decode_event_frames(frames: list[bytes]) -> dict[str, Any]:
    for frame in reversed(frames):
        try:
            payload = json.loads(frame.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("type"), str):
            return payload
    raise ValueError("event frames did not contain a JSON event envelope")
