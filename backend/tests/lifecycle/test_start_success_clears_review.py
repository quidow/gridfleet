"""D5: a node that demonstrably started is not left shelved by the episode that
just ended — and an unrelated review flag is never touched."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.devices.models import DeviceOperationalState
from app.devices.services.review import ReviewService
from app.lifecycle.services import remediation_log
from app.lifecycle.services.actions import reset_reconciler_start_failure_if_needed
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
