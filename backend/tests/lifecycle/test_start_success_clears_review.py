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

    from app.devices.models import Device
    from app.hosts.models import Host

SETTINGS = FakeSettingsReader({"general.lifecycle_recovery_review_threshold": 1})
SETTINGS_TWO_RUNGS = FakeSettingsReader({"general.lifecycle_recovery_review_threshold": 2})


async def _device(db_session: AsyncSession, host_id: object, name: str) -> Device:
    return await create_device(
        db_session,
        host_id=host_id,
        name=name,
        identity_value=f"{name}-001",
        operational_state=DeviceOperationalState.available,
    )


@pytest.mark.db
async def test_start_success_clears_a_review_flag_from_the_same_episode(
    db_session: AsyncSession, db_host: Host
) -> None:
    device = await _device(db_session, db_host.id, "review-same")
    outcome = await escalate_device_remediation_failure(
        db_session, device, settings=SETTINGS, source="appium_reconciler", reason="port_conflict"
    )
    await db_session.commit()
    assert outcome.shelved is True
    assert device.review_required is True

    reset = await reset_reconciler_start_failure_if_needed(db_session, device)
    await db_session.commit()

    assert reset is True
    assert device.review_required is False
    assert device.review_reason is None
    assert (await remediation_log.load_ladder(db_session, device.id)).attempts == 0


@pytest.mark.db
async def test_start_success_clears_the_flag_this_episode_raised_after_further_attempts(
    db_session: AsyncSession, db_host: Host
) -> None:
    """The legitimate case, with the shelving marker no longer the newest entry.

    The ladder shelves a clean device on its second rung and keeps failing
    afterwards. ``review_shelved`` is a property of the whole post-reset window,
    not of the last entry, so the eventual success must still clear the flag.
    """
    device = await _device(db_session, db_host.id, "review-legit")
    for _ in range(3):
        await escalate_device_remediation_failure(
            db_session, device, settings=SETTINGS_TWO_RUNGS, source="appium_reconciler", reason="port_conflict"
        )
    await db_session.commit()
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert device.review_required is True
    assert ladder.attempts == 3, "the marker must not be the newest entry"
    assert ladder.review_shelved is True

    reset = await reset_reconciler_start_failure_if_needed(db_session, device, ladder=ladder)
    await db_session.commit()

    assert reset is True
    assert device.review_required is False
    assert device.review_reason is None


@pytest.mark.db
async def test_start_success_leaves_an_unrelated_review_flag_alone(db_session: AsyncSession, db_host: Host) -> None:
    device = await _device(db_session, db_host.id, "review-other")
    await remediation_log.append_attempt(
        db_session, device.id, source="appium_reconciler", reason="port_conflict", settings=SETTINGS
    )
    await ReviewService().mark_review_required(db_session, device, reason="verification_failed", source="verification")
    await db_session.commit()

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
    device = await _device(db_session, db_host.id, "review-overwritten")
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


@pytest.mark.db
async def test_start_success_keeps_a_verification_shelving_that_landed_inside_the_episode(
    db_session: AsyncSession, db_host: Host
) -> None:
    """The in-window ordering that made both timestamp proxies agree.

    The episode opens FIRST, so ``window_started_at`` precedes the verification
    shelving; then the ladder escalates past the threshold and overwrites
    ``review_reason`` in place. Reason equality holds and
    ``review_set_at >= window_started_at`` holds — yet the device was shelved by
    verification, not by this ladder, and a successful start is no evidence that
    it would pass verification now.
    """
    device = await _device(db_session, db_host.id, "review-in-window")

    # 1. The reconciler episode opens. Threshold is 2, so this rung does not shelve.
    first = await escalate_device_remediation_failure(
        db_session, device, settings=SETTINGS_TWO_RUNGS, source="appium_reconciler", reason="port_conflict"
    )
    assert first.shelved is False
    await db_session.flush()
    window_started_at = (await remediation_log.load_ladder(db_session, device.id)).window_started_at

    # 2. Verification fails and shelves the device, inside the open window.
    assert (
        await ReviewService().mark_review_required(
            db_session,
            device,
            reason="verification failed: driver did not respond",
            source="verification",
        )
        is True
    )
    await db_session.flush()
    assert window_started_at is not None
    assert device.review_set_at is not None
    assert device.review_set_at >= window_started_at, "the shelving must land inside the episode window"

    # 3. The ladder crosses the threshold on an already-flagged device: the
    #    re-flag is a no-op that only overwrites the reason in place.
    second = await escalate_device_remediation_failure(
        db_session, device, settings=SETTINGS_TWO_RUNGS, source="appium_reconciler", reason="port_conflict"
    )
    assert second.shelved is True
    assert device.review_reason == "port_conflict", "the overwrite must genuinely happen"
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.last_failure_reason == device.review_reason, "both timestamp/reason proxies now agree"
    assert ladder.review_shelved is False, "no episode of this ladder raised the flag"

    # 4. The conflict clears and the node starts.
    reset = await reset_reconciler_start_failure_if_needed(db_session, device, ladder=ladder)
    await db_session.commit()

    assert reset is True, "the reconciler episode should still be reset"
    assert device.review_required is True, "a verification shelving must survive a start success"
    assert device.review_reason == "port_conflict"
