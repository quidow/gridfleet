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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

import app.sessions.live_session_predicate as session_axis
from app.devices.models import Device
from app.devices.services import claims
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import (
    VERIFICATION_OUTCOME_KEY,
    VERIFICATION_OUTCOME_PASSED,
    CommandKind,
    IntentRegistration,
    verification_intent_source,
)
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

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
    "devices/services/decision_snapshot.py",  # loads the raw intent column; lease composition delegates to claims
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


@pytest.mark.db
async def test_verification_lease_sql_predicate_matches_row_helper(db_session: AsyncSession, db_host: Host) -> None:
    now = datetime.now(UTC)
    device = await create_device(db_session, host_id=db_host.id, name="claims-contract-verification")
    registered = await IntentService(db_session).register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=verification_intent_source(device.id),
                kind=CommandKind.verification_start,
                payload={},
                expires_at=now + timedelta(minutes=5),
            )
        ],
    )
    await db_session.flush()

    selected = (
        await db_session.execute(
            select(Device.id).where(Device.id == device.id, claims.verification_lease_exists(now=now))
        )
    ).scalar_one_or_none()
    assert selected == device.id
    assert await claims.device_has_verification_lease(db_session, device.id, now=now) is True

    registered[0].expires_at = now - timedelta(seconds=1)
    await db_session.flush()

    selected = (
        await db_session.execute(
            select(Device.id).where(Device.id == device.id, claims.verification_lease_exists(now=now))
        )
    ).scalar_one_or_none()
    assert selected is None
    assert await claims.device_has_verification_lease(db_session, device.id, now=now) is False

    # A terminal outcome stamp tombstones the lease even while unexpired (WS-15.3).
    registered[0].expires_at = now + timedelta(minutes=5)
    registered[0].payload = {**registered[0].payload, VERIFICATION_OUTCOME_KEY: VERIFICATION_OUTCOME_PASSED}
    await db_session.flush()

    selected = (
        await db_session.execute(
            select(Device.id).where(Device.id == device.id, claims.verification_lease_exists(now=now))
        )
    ).scalar_one_or_none()
    assert selected is None
    assert await claims.device_has_verification_lease(db_session, device.id, now=now) is False
