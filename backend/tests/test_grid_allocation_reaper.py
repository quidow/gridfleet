"""Reaper loop cycle: expired pending sessions fail, stale tickets expire, metrics move."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
from app.devices.services.intent import IntentService
from app.grid.allocation import GRID_ALLOCATION_OUTCOME_TOTAL, GRID_QUEUE_DEPTH, AllocationService
from app.grid.allocation_reaper import GridAllocationReaperLoop
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.grid.services_container import GridServices
from app.sessions.models import Session, SessionStatus
from tests.conftest import settings_service
from tests.helpers import seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus
from tests.pack.factories import seed_test_packs


def _body(**caps: str) -> dict[str, Any]:
    return {"capabilities": {"alwaysMatch": caps, "firstMatch": [{}]}}


async def _stereotype_stub(db: AsyncSession, device: Device, *, template_cache: object | None = None) -> dict[str, Any]:
    return {"platformName": "Android"}


class _SettingsStub:
    def get(self, key: str) -> int:
        return {
            "grid.claim_window_sec": 30,
            "grid.queue_timeout_sec": 300,
            "general.session_viability_timeout_sec": 120,
        }[key]


@pytest.fixture
def allocation_service() -> AllocationService:
    return AllocationService(
        intent_factory=IntentService,
        publisher=event_bus,
        stereotype_provider=_stereotype_stub,
        settings=_SettingsStub(),
    )


@pytest.fixture
def reaper(db_session: AsyncSession, allocation_service: AllocationService) -> GridAllocationReaperLoop:
    assert db_session.bind is not None
    from sqlalchemy.ext.asyncio import async_sessionmaker

    services = GridServices(
        settings=settings_service,
        session_factory=async_sessionmaker(db_session.bind),
        allocation=allocation_service,
    )
    return GridAllocationReaperLoop(services=services)


@pytest_asyncio.fixture
async def expired_pending_session(db_session: AsyncSession, allocation_service: AllocationService) -> Session:
    await seed_test_packs(db_session)
    await seed_host_and_running_node(db_session, identity=f"grid-reap-{uuid.uuid4().hex[:8]}")
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    result = await allocation_service.try_allocate(db_session, ticket=ticket)
    assert result is not None
    row = await db_session.get(Session, result.allocation_id)
    assert row is not None
    row.started_at = datetime.now(UTC) - timedelta(seconds=120)
    await db_session.flush()
    return row


@pytest.mark.db
async def test_reaper_cycle_fails_expired_pending(
    db_session: AsyncSession, expired_pending_session: Session, reaper: GridAllocationReaperLoop
) -> None:
    stale_ticket = GridSessionQueueTicket(
        requested_body=_body(platformName="Android"),
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add(stale_ticket)
    await db_session.flush()
    claim_expired_before = GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="claim_expired")._value.get()
    expired_before = GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="expired")._value.get()

    await reaper.run_cycle(db_session)

    await db_session.refresh(expired_pending_session)
    assert expired_pending_session.status == SessionStatus.error
    await db_session.refresh(stale_ticket)
    assert stale_ticket.status == GridQueueStatus.expired
    assert GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="claim_expired")._value.get() == claim_expired_before + 1
    assert GRID_ALLOCATION_OUTCOME_TOTAL.labels(outcome="expired")._value.get() == expired_before + 1
    assert GRID_QUEUE_DEPTH._value.get() == 0


@pytest.mark.db
async def test_reaper_wakes_session_sync_after_failing_pending(
    db_session: AsyncSession,
    expired_pending_session: Session,
    reaper: GridAllocationReaperLoop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: failing a reaped pending row frees its device; the reaper must ring the
    session_sync doorbell so the orphan/liveness sweep runs immediately instead of up to
    one poll interval later (racing a router-crash orphan re-allocation)."""
    woke: list[bool] = []
    monkeypatch.setattr(
        "app.grid.allocation_reaper.request_session_sync_wake",
        lambda: woke.append(True),
    )

    await reaper.run_cycle(db_session)

    await db_session.refresh(expired_pending_session)
    assert expired_pending_session.status == SessionStatus.error
    assert woke == [True]


@pytest.mark.db
async def test_reaper_does_not_wake_session_sync_when_no_pending_freed(
    db_session: AsyncSession,
    reaper: GridAllocationReaperLoop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wake fires only when a pending row was actually failed: expiring a stale ticket
    (no device freed) must not ring the doorbell."""
    await seed_test_packs(db_session)
    stale = GridSessionQueueTicket(
        requested_body=_body(platformName="Android"),
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add(stale)
    await db_session.flush()
    woke: list[bool] = []
    monkeypatch.setattr(
        "app.grid.allocation_reaper.request_session_sync_wake",
        lambda: woke.append(True),
    )

    await reaper.run_cycle(db_session)

    await db_session.refresh(stale)
    assert stale.status == GridQueueStatus.expired
    assert woke == []


@pytest.mark.db
async def test_reaper_expires_stale_polled_ticket_before_queue_timeout(
    db_session: AsyncSession, reaper: GridAllocationReaperLoop
) -> None:
    """C8: a young (within queue_timeout) waiting ticket whose client stopped polling
    is expired by the reaper on liveness, not after the full 300s queue timeout."""
    stale = GridSessionQueueTicket(
        requested_body=_body(platformName="Android"),
        created_at=datetime.now(UTC) - timedelta(seconds=30),  # well within the 300s queue timeout
        last_polled_at=datetime.now(UTC) - timedelta(seconds=60),  # >> 10x1s liveness window
    )
    db_session.add(stale)
    await db_session.flush()

    await reaper.run_cycle(db_session)

    await db_session.refresh(stale)
    assert stale.status == GridQueueStatus.expired
