"""D5: a node that demonstrably started is not left shelved by the episode that
just ended — and an unrelated review flag is never touched."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.devices.models import DeviceOperationalState
from app.devices.services.review import ReviewService
from app.lifecycle.services import remediation_log
from app.lifecycle.services.actions import (
    escalate_device_remediation_failure,
    reset_reconciler_start_failure_if_needed,
)
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

SETTINGS = FakeSettingsReader({"general.lifecycle_recovery_review_threshold": 1})


async def _shelved_device(db_session: AsyncSession, host_id: object, name: str, *, review_reason: str) -> object:
    device = await create_device(
        db_session,
        host_id=host_id,
        name=name,
        identity_value=f"{name}-001",
        operational_state=DeviceOperationalState.available,
    )
    await remediation_log.append_attempt(
        db_session, device.id, source="appium_reconciler", reason="port_conflict", settings=SETTINGS
    )
    await ReviewService().mark_review_required(db_session, device, reason=review_reason, source="appium_reconciler")
    await db_session.commit()
    return device


@pytest.mark.db
async def test_start_success_clears_a_review_flag_from_the_same_episode(
    db_session: AsyncSession, db_host: Host
) -> None:
    device = await _shelved_device(db_session, db_host.id, "review-same", review_reason="port_conflict")
    assert device.review_required is True

    reset = await reset_reconciler_start_failure_if_needed(db_session, device)
    await db_session.commit()

    assert reset is True
    assert device.review_required is False
    assert device.review_reason is None
    assert (await remediation_log.load_ladder(db_session, device.id)).attempts == 0


@pytest.mark.db
async def test_start_success_leaves_an_unrelated_review_flag_alone(db_session: AsyncSession, db_host: Host) -> None:
    device = await _shelved_device(db_session, db_host.id, "review-other", review_reason="verification_failed")

    reset = await reset_reconciler_start_failure_if_needed(db_session, device)
    await db_session.commit()

    assert reset is True, "the reconciler episode should still be reset"
    assert device.review_required is True, "an unrelated shelving must survive"
    assert device.review_reason == "verification_failed"


@pytest.mark.db
async def test_start_success_keeps_a_verification_shelving_the_ladder_overwrote(
    db_session: AsyncSession, db_host: Host
) -> None:
    """The review reason alone cannot scope the clear.

    ``mark_review_required`` overwrites ``review_reason`` in place on an
    already-flagged device, so a verification shelving that predates the ladder
    episode ends up wearing the ladder's own reason. Clearing on that match
    returns a device that never passed verification to the allocatable pool.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="review-overwritten",
        identity_value="review-overwritten-001",
        operational_state=DeviceOperationalState.available,
    )
    # 1. Verification shelves the device. No ladder attempt is written.
    await ReviewService().mark_review_required(
        db_session,
        device,
        reason="verification failed: driver did not respond",
        source="verification",
    )
    await db_session.flush()
    shelved_at = device.review_set_at

    # 2. A later reconciler episode escalates past the review threshold and
    #    overwrites the reason in place (review_set_at is deliberately kept).
    outcome = await escalate_device_remediation_failure(
        db_session,
        device,
        settings=SETTINGS,
        source="appium_reconciler",
        reason="port_conflict",
    )
    assert outcome.shelved is True
    assert device.review_reason == "port_conflict", "the overwrite must genuinely happen"
    assert device.review_set_at == shelved_at, "the initial flag-on time must not be refreshed"

    # 3. The conflict clears and the node starts.
    reset = await reset_reconciler_start_failure_if_needed(db_session, device)
    await db_session.commit()

    assert reset is True, "the reconciler episode should still be reset"
    assert device.review_required is True, "a shelving that predates the episode must survive"
    assert device.review_reason == "port_conflict"
