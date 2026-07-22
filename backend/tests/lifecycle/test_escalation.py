"""Unit-level coverage for the shared remediation-escalation ladder."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.core.timeutil import now_utc
from app.lifecycle.services import remediation_log
from app.lifecycle.services.escalation import escalate_remediation_failure
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


pytestmark = pytest.mark.db

SETTINGS = FakeSettingsReader(
    {
        "general.lifecycle_recovery_backoff_base_sec": 60,
        "general.lifecycle_recovery_backoff_max_sec": 900,
        "general.lifecycle_recovery_review_threshold": 3,
    }
)


async def test_escalate_increments_attempts_and_arms_backoff(db_session: AsyncSession, db_host: Host) -> None:
    review = AsyncMock()
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="escalation-attempts",
        name="escalation-attempts",
    )

    first = await escalate_remediation_failure(
        db_session,
        device,
        settings=SETTINGS,
        review=review,
        source="node_health",
        reason="first failure",
    )
    second = await escalate_remediation_failure(
        db_session,
        device,
        settings=SETTINGS,
        review=review,
        source="node_health",
        reason="second failure",
    )

    assert (first.attempts, second.attempts) == (1, 2)
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.backoff_active(now=now_utc()) is not None
    assert ladder.last_failure_reason == "second failure"
    assert first.shelved is False and second.shelved is False
    review.mark_review_required.assert_not_awaited()


async def test_escalate_promotes_to_review_at_threshold(db_session: AsyncSession, db_host: Host) -> None:
    review = AsyncMock()
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="escalation-threshold",
        name="escalation-threshold",
    )

    for _ in range(2):
        await escalate_remediation_failure(
            db_session,
            device,
            settings=SETTINGS,
            review=review,
            source="node_health",
            reason="kept failing",
        )
    outcome = await escalate_remediation_failure(
        db_session,
        device,
        settings=SETTINGS,
        review=review,
        source="node_health",
        reason="kept failing",
    )

    assert outcome.shelved is True
    review.mark_review_required.assert_awaited_once_with(
        db_session,
        device,
        reason="kept failing",
        source="node_health",
    )


def test_backoff_active_treats_a_past_deadline_as_expired() -> None:
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    ladder = remediation_log.LadderState(1, now, None, None, None, None)

    assert ladder.backoff_active(now=now) is None


async def test_escalate_remediation_failure_with_prior_ladder_skips_select(
    db_session: AsyncSession, db_host: Host
) -> None:
    from app.lifecycle.services.remediation_log import EMPTY_LADDER
    from tests.concurrency.group_lock_helpers import capture_statements
    from tests.fakes.review import build_review_service

    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="escalation-prior",
        name="escalation-prior",
    )
    await db_session.commit()

    review = build_review_service()

    async with capture_statements(db_session) as statements:
        outcome = await escalate_remediation_failure(
            db_session,
            device,
            settings=SETTINGS,
            review=review,
            source="test",
            reason="failed",
            prior=EMPTY_LADDER,
        )

    reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 0, f"Expected no SELECT statements, got {reads}"
    assert outcome.ladder.attempts == 1
