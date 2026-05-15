"""Shared constants for the pack router and schemas."""

from __future__ import annotations

# Single-segment identifiers: alphanumeric, underscores, dots, hyphens; no slashes.
PLATFORM_ID_PATTERN = r"^[A-Za-z0-9_.\-]+$"

# Slash-separated pack-id segments. Single-dot and double-dot traversal segments
# are rejected without lookahead for pydantic-core regex compatibility.
PACK_ID_PATTERN = (
    r"^(?:[A-Za-z0-9_.\-]*[A-Za-z0-9_\-][A-Za-z0-9_.\-]*|\.{3,})"
    r"(?:/(?:[A-Za-z0-9_.\-]*[A-Za-z0-9_\-][A-Za-z0-9_.\-]*|\.{3,}))*$"
)
