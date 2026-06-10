from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.devices.models import DeviceReservation
from app.devices.services.data_cleanup import DataCleanupService
from app.events.models import SystemEvent
from app.jobs.models import Job
from app.jobs.statuses import JOB_STATUS_COMPLETED, JOB_STATUS_PENDING
from app.runs.models import RunState, TestRun
from app.webhooks.models import Webhook, WebhookDelivery
from tests.fakes import FakeSettingsReader
from tests.helpers import create_reservation, seed_host_and_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.db

_REQUIREMENTS = [{"platform_id": "android_mobile", "count": 1}]


async def _run_cleanup(db_session: AsyncSession) -> None:
    await DataCleanupService(publisher=AsyncMock(), settings=FakeSettingsReader({})).cleanup_old_data(db_session)


@pytest.mark.asyncio
async def test_old_system_events_pruned_and_webhook_deliveries_cascade(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    old_event = SystemEvent(type="system.test", data={}, created_at=now - timedelta(days=40))
    new_event = SystemEvent(type="system.test", data={}, created_at=now - timedelta(days=1))
    webhook = Webhook(name="t", url="https://example.test/hook", event_types=["system.test"], enabled=True)
    db_session.add_all([old_event, new_event, webhook])
    await db_session.flush()
    db_session.add_all(
        [
            WebhookDelivery(
                webhook_id=webhook.id, system_event_id=old_event.id, event_type="system.test", status="delivered"
            ),
            WebhookDelivery(
                webhook_id=webhook.id, system_event_id=new_event.id, event_type="system.test", status="delivered"
            ),
        ]
    )
    await db_session.commit()
    old_event_id, new_event_id = old_event.id, new_event.id

    await _run_cleanup(db_session)

    remaining_events = set(
        (
            await db_session.execute(select(SystemEvent.id).where(SystemEvent.id.in_([old_event_id, new_event_id])))
        ).scalars()
    )
    assert remaining_events == {new_event_id}
    remaining_deliveries = set(
        (
            await db_session.execute(
                select(WebhookDelivery.system_event_id).where(
                    WebhookDelivery.system_event_id.in_([old_event_id, new_event_id])
                )
            )
        ).scalars()
    )
    assert remaining_deliveries == {new_event_id}


@pytest.mark.asyncio
async def test_only_old_terminal_test_runs_pruned_and_reservations_cascade(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    _, device = await seed_host_and_device(db_session, identity="cleanup-run-dev")
    old_terminal = TestRun(
        name="old-done", state=RunState.completed, requirements=_REQUIREMENTS, created_at=now - timedelta(days=40)
    )
    old_active = TestRun(
        name="old-active", state=RunState.active, requirements=_REQUIREMENTS, created_at=now - timedelta(days=40)
    )
    new_terminal = TestRun(
        name="new-done", state=RunState.completed, requirements=_REQUIREMENTS, created_at=now - timedelta(days=1)
    )
    db_session.add_all([old_terminal, old_active, new_terminal])
    await db_session.flush()
    await create_reservation(db_session, device_id=device.id, run_id=old_terminal.id)
    await db_session.commit()
    run_ids = [old_terminal.id, old_active.id, new_terminal.id]
    old_terminal_id, old_active_id, new_terminal_id = run_ids

    await _run_cleanup(db_session)

    remaining_runs = set((await db_session.execute(select(TestRun.id).where(TestRun.id.in_(run_ids)))).scalars())
    assert remaining_runs == {old_active_id, new_terminal_id}
    remaining_reservations = (
        (await db_session.execute(select(DeviceReservation.id).where(DeviceReservation.run_id == old_terminal_id)))
        .scalars()
        .all()
    )
    assert remaining_reservations == []


@pytest.mark.asyncio
async def test_only_old_terminal_jobs_pruned(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    old_done = Job(kind="cleanup-test", status=JOB_STATUS_COMPLETED, created_at=now - timedelta(days=40))
    old_pending = Job(kind="cleanup-test", status=JOB_STATUS_PENDING, created_at=now - timedelta(days=40))
    new_done = Job(kind="cleanup-test", status=JOB_STATUS_COMPLETED, created_at=now - timedelta(days=1))
    db_session.add_all([old_done, old_pending, new_done])
    await db_session.commit()
    job_ids = [old_done.id, old_pending.id, new_done.id]
    _old_done_id, old_pending_id, new_done_id = job_ids

    await _run_cleanup(db_session)

    remaining_jobs = set((await db_session.execute(select(Job.id).where(Job.id.in_(job_ids)))).scalars())
    assert remaining_jobs == {old_pending_id, new_done_id}
