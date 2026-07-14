"""Per-section dedup token stamped on the two moved health sections.

The backend compares ``(boot_id, section_sequence, payload_sha256)`` against its
per-section ingest cursor to dedup re-deliveries. ``canonical_section_hash`` MUST
byte-for-byte match ``app/hosts/observation_token.py`` on the backend — the
shared parity test in the backend suite pins this.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Keys added to a section that are NOT part of the hashed body (must match the
# backend). ``observation_revision`` is stamped by the backend, never the agent.
TOKEN_KEYS = frozenset({"section_sequence", "payload_sha256", "observation_revision"})

SECTION_SEQUENCE_KEY = "section_sequence"
PAYLOAD_SHA256_KEY = "payload_sha256"


def canonical_section_hash(section: dict[str, Any]) -> str:
    """SHA-256 over the section body with the token/stamp keys removed, serialized
    with sorted keys and compact separators."""
    body = {key: value for key, value in section.items() if key not in TOKEN_KEYS}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
