"""Claim-predicate composition contract (WS-6.1).

Each device-claim axis has exactly one SQL "active" definition, homed in
``app.devices.services.claims`` (the session axis is defined in
``app.sessions.live_session_predicate`` and re-exported). Hand-recomposing an
axis at a call site is the drift class that produced the pending-omission bug
(see the live_session_predicate module docstring); these scans keep new copies
from appearing. Writes to the underlying rows are owner concerns and are not
matched by these patterns.
"""

from __future__ import annotations

import re
from pathlib import Path

import app.sessions.live_session_predicate as session_axis
from app.devices.services import claims

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

# SQL reads of DeviceReservation.released_at (the reservation-axis definition).
RELEASED_AT_PATTERN = re.compile(r"\.released_at\s*\.\s*(is_|is_not|isnot)\s*\(")
RELEASED_AT_ALLOWED = {
    "devices/services/claims.py",  # the axis home
    "devices/__init__.py",  # Core-table metrics gauge; avoids ORM import at package import
}

# SQL reads of DeviceIntent.expires_at (the verification-lease definition).
EXPIRES_AT_PATTERN = re.compile(r"DeviceIntent\.expires_at")
EXPIRES_AT_ALLOWED = {
    "devices/services/claims.py",  # the axis home
    "devices/services/intent_reconciler.py",  # expired-intent GC (lease lifecycle, not claim gating)
    "lifecycle/services/operator_node.py",  # sticky operator-stop intent, a separate axis
}

# The live-status pair spelled inline (the shape of the original bug).
LIVE_PAIR_PATTERN = re.compile(r"Session\.status.*SessionStatus\.(pending|running).*SessionStatus\.(pending|running)")
LIVE_PAIR_ALLOWED = {
    "sessions/live_session_predicate.py",  # the axis home (_LIVE_STATUSES)
}


def _violations(pattern: re.Pattern[str], allowed: set[str]) -> list[str]:
    found: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(APP_ROOT).as_posix()
        if rel in allowed:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.search(line):
                found.append(f"app/{rel}:{lineno}: {line.strip()}")
    return found


def test_session_axis_is_reexported_not_copied() -> None:
    assert claims.live_session_predicate is session_axis.live_session_predicate
    assert claims.device_has_live_session is session_axis.device_has_live_session


def test_reservation_active_definition_is_single_homed() -> None:
    assert _violations(RELEASED_AT_PATTERN, RELEASED_AT_ALLOWED) == []


def test_verification_lease_definition_is_single_homed() -> None:
    assert _violations(EXPIRES_AT_PATTERN, EXPIRES_AT_ALLOWED) == []


def test_live_status_pair_is_not_recomposed_inline() -> None:
    assert _violations(LIVE_PAIR_PATTERN, LIVE_PAIR_ALLOWED) == []
