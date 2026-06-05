"""AllocationService lifecycle: confirm, fail, mark_ended, reap_expired."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import Device, DeviceOperationalState
from app.devices.services.intent import IntentService
from app.grid.allocation import AllocationNotPendingError, AllocationService
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.sessions.models import Session, SessionStatus
from tests.helpers import seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus
from tests.pack.factories import seed_test_packs


def _body(**caps: str) -> dict[str, Any]:
    return {"capabilities": {"alwaysMatch": caps, "firstMatch": [{}]}}


async def _stereotype_stub(db: AsyncSession, device: Device) -> dict[str, Any]:
    return {"platformName": "Android"}


class _SettingsStub:
    def __init__(self, values: dict[str, int]) -> None:
        self._values = values

    def get(self, key: str) -> int:
        return self._values[key]


@pytest.fixture
def allocation_service() -> AllocationService:
    return AllocationService(
        intent_factory=IntentService,
        publisher=event_bus,
        stereotype_provider=_stereotype_stub,
        settings=_SettingsStub({"grid.claim_window_sec": 30, "grid.queue_timeout_sec": 300}),
    )


@pytest_asyncio.fixture
async def seeded_available_device(db_session: AsyncSession) -> Device:
    await seed_test_packs(db_session)
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"grid-lc-{uuid.uuid4().hex[:8]}")
    return device


@pytest_asyncio.fixture
async def allocated_pending(
    db_session: AsyncSession, seeded_available_device: Device, allocation_service: AllocationService
) -> Session:
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    result = await allocation_service.try_allocate(db_session, ticket=ticket)
    assert result is not None
    row = await db_session.get(Session, result.allocation_id)
    assert row is not None
    return row


@pytest.mark.db
async def test_confirm_promotes_to_running(
    db_session: AsyncSession, allocated_pending: Session, allocation_service: AllocationService
) -> None:
    await allocation_service.confirm(db_session, allocation_id=allocated_pending.id, appium_session_id="abc123")
    await db_session.refresh(allocated_pending)
    assert allocated_pending.status == SessionStatus.running
    assert allocated_pending.session_id == "abc123"


@pytest.mark.db
async def test_confirm_non_pending_raises(
    db_session: AsyncSession, allocated_pending: Session, allocation_service: AllocationService
) -> None:
    await allocation_service.confirm(db_session, allocation_id=allocated_pending.id, appium_session_id="abc123")
    with pytest.raises(AllocationNotPendingError):
        await allocation_service.confirm(db_session, allocation_id=allocated_pending.id, appium_session_id="dup")
    with pytest.raises(AllocationNotPendingError):
        await allocation_service.confirm(db_session, allocation_id=uuid.uuid4(), appium_session_id="missing")


@pytest.mark.db
async def test_fail_releases_device(
    db_session: AsyncSession,
    allocated_pending: Session,
    allocation_service: AllocationService,
    seeded_available_device: Device,
) -> None:
    await allocation_service.fail(db_session, allocation_id=allocated_pending.id, message="appium refused")
    await db_session.refresh(allocated_pending)
    assert allocated_pending.status == SessionStatus.error
    assert allocated_pending.error_type == "allocation_failed"
    assert allocated_pending.ended_at is not None
    await db_session.refresh(seeded_available_device)
    assert seeded_available_device.operational_state == DeviceOperationalState.available
    # idempotent: failing again (or failing a confirmed/unknown id) is a no-op
    await allocation_service.fail(db_session, allocation_id=allocated_pending.id, message="again")
    await allocation_service.fail(db_session, allocation_id=uuid.uuid4(), message="missing")


@pytest.mark.db
async def test_mark_ended_closes_running_and_frees_device(
    db_session: AsyncSession,
    allocated_pending: Session,
    allocation_service: AllocationService,
    seeded_available_device: Device,
) -> None:
    await allocation_service.confirm(db_session, allocation_id=allocated_pending.id, appium_session_id="end-me")
    await allocation_service.mark_ended(db_session, appium_session_id="end-me")
    await db_session.refresh(allocated_pending)
    assert allocated_pending.ended_at is not None
    assert allocated_pending.status == SessionStatus.passed
    await db_session.refresh(seeded_available_device)
    assert seeded_available_device.operational_state == DeviceOperationalState.available
    # unknown session id is a no-op
    await allocation_service.mark_ended(db_session, appium_session_id="never-existed")


@pytest.mark.db
async def test_reap_expired_pending_and_tickets(
    db_session: AsyncSession, allocated_pending: Session, allocation_service: AllocationService
) -> None:
    allocated_pending.started_at = datetime.now(UTC) - timedelta(seconds=120)
    stale_ticket = GridSessionQueueTicket(
        requested_body=_body(platformName="Android"),
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    fresh_ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add_all([stale_ticket, fresh_ticket])
    await db_session.flush()

    reaped = await allocation_service.reap_expired(db_session)

    assert reaped == {"pending_failed": 1, "tickets_expired": 1}
    await db_session.refresh(allocated_pending)
    assert allocated_pending.status == SessionStatus.error
    await db_session.refresh(stale_ticket)
    assert stale_ticket.status == GridQueueStatus.expired
    await db_session.refresh(fresh_ticket)
    assert fresh_ticket.status == GridQueueStatus.waiting
