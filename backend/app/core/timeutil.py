"""Shared time helpers.

``parse_iso`` and ``now_utc`` were copy-pasted byte-for-byte across the lifecycle,
sessions, runs, and observability packages (Q10). Promoting them here gives one
implementation every caller derives from so the ISO parse (including the legacy
``Z`` suffix) and the UTC clock cannot drift.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now_utc() -> datetime:
    """Timezone-aware current time in UTC."""
    return datetime.now(UTC)


def parse_iso(raw: object) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z`` (UTC) suffix.

    Returns ``None`` for missing / non-string / unparseable input. An input that is
    already a ``datetime`` is returned as-is (some JSON-column readers pass through a
    pre-parsed value).
    """
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
