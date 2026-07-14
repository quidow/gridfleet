"""Per-section dedup token for the two moved health folds.

The agent stamps each moved section (``node_health``, ``device_health``) with a
token ``(boot_id, section_sequence, payload_sha256)``:

- ``boot_id`` (top-level on the push) identifies the agent boot.
- ``section_sequence`` is a per-``(boot, section)`` counter the agent bumps once
  per *gather* (not per push), so a re-delivery of the same gather carries the
  same sequence.
- ``payload_sha256`` is the canonical hash of the section body (everything but
  the token/stamp keys), computed identically on both sides so the backend can
  reject a corrupted body and detect a same-sequence/different-payload contract
  violation.

The backend recomputes the hash and compares the token against the host's
per-section ingest cursor to decide whether a section is a genuinely new
generation (draw a fresh revision) or a re-delivery (reuse the stamped one).
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import uuid

# Keys the agent/backend add to a section that are NOT part of the hashed body.
TOKEN_KEYS = frozenset({"section_sequence", "payload_sha256", "observation_revision", "observation_received_at"})

SECTION_SEQUENCE_KEY = "section_sequence"
PAYLOAD_SHA256_KEY = "payload_sha256"


def canonical_section_hash(section: dict[str, Any]) -> str:
    """SHA-256 over the section body with the token/stamp keys removed, serialized
    with sorted keys and compact separators. Agent and backend MUST agree."""
    body = {key: value for key, value in section.items() if key not in TOKEN_KEYS}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SectionToken:
    """The token carried by a moved section, if the agent stamped one."""

    __slots__ = ("boot_id", "payload_sha256", "section_sequence")

    def __init__(self, *, boot_id: str, section_sequence: int, payload_sha256: str) -> None:
        self.boot_id = boot_id
        self.section_sequence = section_sequence
        self.payload_sha256 = payload_sha256


def extract_token(section: dict[str, Any], *, boot_id: uuid.UUID | None) -> SectionToken | None:
    """Return the section's token, or ``None`` for a tokenless/legacy section
    (missing boot_id or section fields ⇒ fall back to at-least-once processing)."""
    if boot_id is None:
        return None
    sequence = section.get(SECTION_SEQUENCE_KEY)
    payload_sha256 = section.get(PAYLOAD_SHA256_KEY)
    # bool is an int subclass in Python, but is not a valid monotonic sequence.
    # Negative counters are likewise malformed and degrade to at-least-once.
    if type(sequence) is not int or sequence < 0 or not isinstance(payload_sha256, str):
        return None
    return SectionToken(boot_id=str(boot_id), section_sequence=sequence, payload_sha256=payload_sha256)
