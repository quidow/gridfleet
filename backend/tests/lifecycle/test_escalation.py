"""Unit-level coverage for the shared remediation-escalation ladder."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.lifecycle.services import remediation_log
from app.lifecycle.services.escalation import escalate_remediation_failure
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device, create_device_record

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


pytestmark = pytest.mark.db

SETTINGS = FakeSettingsReader(
    {
        "general.lifecycle_recovery_backoff_base_sec": 60,
        "general.lifecycle_recovery_backoff_max_sec": 900,
    }
)


async def test_escalate_increments_attempts_and_arms_backoff(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="escalation-attempts",
        name="escalation-attempts",
    )
    locked = await device_locking.lock_device_handle(db_session, device.id)

    first = await escalate_remediation_failure(
        db_session,
        locked,
        settings=SETTINGS,
        source="node_health",
        reason="first failure",
    )
    second = await escalate_remediation_failure(
        db_session,
        locked,
        settings=SETTINGS,
        source="node_health",
        reason="second failure",
    )

    assert (first.attempts, second.attempts) == (1, 2)
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.backoff_active(now=now_utc()) is not None
    assert ladder.last_failure_reason == "second failure"


async def test_escalation_caps_backoff_and_never_stops(db_session: AsyncSession, db_host: Host) -> None:
    """Attempts keep accruing past any old threshold; backoff saturates at the cap.

    Expected sequence re-derived from append_attempt: seconds = min(900, 60 * 2**(n-1))
    for n = 1..7 -> 60, 120, 240, 480, 900, 900, 900.
    """
    device = await create_device(db_session, host_id=db_host.id, name="cap-forever")
    locked = await device_locking.lock_device_handle(db_session, device.id)
    settings = FakeSettingsReader(
        {
            "general.lifecycle_recovery_backoff_base_sec": 60,
            "general.lifecycle_recovery_backoff_max_sec": 900,
        }
    )
    delays: list[int] = []
    ladder = None
    outcome = None
    for _ in range(7):
        before = now_utc()
        outcome = await escalate_remediation_failure(
            db_session, locked, settings=settings, source="node_health", reason="probe failed", prior=ladder
        )
        ladder = outcome.ladder
        assert ladder.backoff_until is not None
        delays.append(round((ladder.backoff_until - before).total_seconds()))
    assert outcome is not None
    assert delays == [60, 120, 240, 480, 900, 900, 900]
    assert outcome.attempts == 7


def test_backoff_active_treats_a_past_deadline_as_expired() -> None:
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    ladder = remediation_log.LadderState(1, now, None, None, None, None)

    assert ladder.backoff_active(now=now) is None


async def test_escalate_remediation_failure_with_prior_ladder_skips_select(
    db_session: AsyncSession, db_host: Host
) -> None:
    from app.lifecycle.services.remediation_log import EMPTY_LADDER
    from tests.concurrency.group_lock_helpers import capture_statements

    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="escalation-prior",
        name="escalation-prior",
    )
    await db_session.commit()

    # The lock's own SELECT ... FOR UPDATE autobegins a transaction, and
    # capture_statements refuses to attach to a session that already has one
    # open (it re-pins itself via after_begin, so it needs a fresh begin to
    # observe) -- so the lock must stay inside the capture window. The
    # assertion below targets the device_remediation_log table specifically,
    # which is the actual property under test (load_ladder's SELECT must be
    # skipped when `prior` is supplied), rather than counting every SELECT
    # the window sees, so it stays exact-zero on that property regardless of
    # the lock's own statement shape.
    async with capture_statements(db_session) as statements:
        locked = await device_locking.lock_device_handle(db_session, device.id)
        outcome = await escalate_remediation_failure(
            db_session,
            locked,
            settings=SETTINGS,
            source="test",
            reason="failed",
            prior=EMPTY_LADDER,
        )

    ladder_reads = [
        sql for sql in statements if sql.lstrip().upper().startswith("SELECT") and "device_remediation_log" in sql
    ]
    assert ladder_reads == [], f"Expected no ladder SELECT (prior was supplied), got {ladder_reads}"
    assert outcome.ladder.attempts == 1
