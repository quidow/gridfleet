from __future__ import annotations

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

T = TypeVar("T")


class CursorPaginationError(ValueError):
    """Raised when an opaque cursor token cannot be decoded."""


@dataclass(slots=True, frozen=True)
class CursorToken:
    timestamp: datetime
    item_id: uuid.UUID


@dataclass(slots=True)
class CursorPage[T]:
    items: list[T]
    limit: int
    next_cursor: str | None
    prev_cursor: str | None


def encode_cursor(timestamp: datetime, item_id: uuid.UUID) -> str:
    payload = {
        "timestamp": timestamp.isoformat(),
        "id": str(item_id),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> CursorToken:
    padding = "=" * (-len(cursor) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{cursor}{padding}")
        payload = json.loads(decoded.decode("utf-8"))
        timestamp = datetime.fromisoformat(payload["timestamp"])
        item_id = uuid.UUID(str(payload["id"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise CursorPaginationError("Invalid cursor token") from exc
    return CursorToken(timestamp=timestamp, item_id=item_id)
