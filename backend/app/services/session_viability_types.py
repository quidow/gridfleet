"""Shared types for session viability probe results.

`SessionViabilityCheckedBy` is the single source of truth for who triggered a
viability probe. Use it on every writer (`record_session_viability_result`,
`run_session_viability_probe`, `_write_session_viability`) and on the public
`SessionViabilityRead` response schema so reader and writer cannot drift.
"""

from __future__ import annotations

from enum import StrEnum


class SessionViabilityCheckedBy(StrEnum):
    scheduled = "scheduled"
    manual = "manual"
    recovery = "recovery"
    verification = "verification"
