"""AllocationService lifecycle: confirm, fail, mark_ended, reap_expired."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.orm import selectinload

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import Device, DeviceOperationalState
from app.devices.services.intent import IntentService
from app.grid.allocation import AllocationNotPendingError, AllocationService
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.runs.models import RunState
from app.sessions.models import Session, SessionStatus
from tests.helpers import create_reserved_run, seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs


def _body(**caps: str) -> dict[str, Any]:
    return {"capabilities": {"alwaysMatch": caps, "firstMatch": [{}]}}


async def _stereotype_stub(db: AsyncSession, device: Device, *, template_cache: object | None = None) -> dict[str, Any]:
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
        settings=_SettingsStub(
            {
                "grid.claim_window_sec": 30,
                "grid.queue_timeout_sec": 300,
                "general.session_viability_timeout_sec": 120,
            }
        ),
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
async def test_claim_stamps_ticket_id(
    db_session: AsyncSession, seeded_available_device: Device, allocation_service: AllocationService
) -> None:
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    result = await allocation_service.try_allocate(db_session, ticket=ticket)
    assert result is not None
    row = await db_session.get(Session, result.allocation_id)
    assert row is not None
    assert row.ticket_id == ticket.id


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
async def test_confirm_after_reaper_fail_raises_not_pending(
    db_session: AsyncSession,
    allocated_pending: Session,
    allocation_service: AllocationService,
    seeded_available_device: Device,
) -> None:
    """Race: the reaper fails the pending row first; confirm must then lose (raise),
    not blindly overwrite it back to running (#4)."""
    await allocation_service.fail(db_session, allocation_id=allocated_pending.id, message="claim window expired")
    await db_session.refresh(allocated_pending)
    assert allocated_pending.status == SessionStatus.error

    with pytest.raises(AllocationNotPendingError):
        await allocation_service.confirm(db_session, allocation_id=allocated_pending.id, appium_session_id="too-late")

    await db_session.refresh(allocated_pending)
    # The reaper's terminal state stands; confirm did not split it back to running.
    assert allocated_pending.status == SessionStatus.error
    assert allocated_pending.ended_at is not None


@pytest.mark.db
async def test_fail_after_confirm_is_noop_device_stays_busy(
    db_session: AsyncSession,
    allocated_pending: Session,
    allocation_service: AllocationService,
    seeded_available_device: Device,
) -> None:
    """Race: confirm wins first; a late reaper fail must no-op rather than error a
    now-running session or free a still-busy device (#4)."""
    await allocation_service.confirm(db_session, allocation_id=allocated_pending.id, appium_session_id="confirmed-id")
    await db_session.refresh(seeded_available_device)
    assert seeded_available_device.operational_state == DeviceOperationalState.busy

    await allocation_service.fail(db_session, allocation_id=allocated_pending.id, message="claim window expired")

    await db_session.refresh(allocated_pending)
    assert allocated_pending.status == SessionStatus.running
    assert allocated_pending.session_id == "confirmed-id"
    assert allocated_pending.ended_at is None
    await db_session.refresh(seeded_available_device)
    assert seeded_available_device.operational_state == DeviceOperationalState.busy


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
async def test_mark_ended_run_terminal_marks_error(
    db_session: AsyncSession,
    allocated_pending: Session,
    allocation_service: AllocationService,
    seeded_available_device: Device,
) -> None:
    """A session whose owning run already reached a non-completed terminal state was
    aborted out from under the client — mark_ended must close it ``error``, not
    mask the abort as ``passed`` (#7)."""
    await allocation_service.confirm(db_session, allocation_id=allocated_pending.id, appium_session_id="aborted-run")
    run = await create_reserved_run(
        db_session, name="cancelled-run", devices=[seeded_available_device], state=RunState.cancelled
    )
    allocated_pending.run_id = run.id
    await db_session.flush()

    await allocation_service.mark_ended(db_session, appium_session_id="aborted-run")

    await db_session.refresh(allocated_pending)
    assert allocated_pending.status == SessionStatus.error
    assert allocated_pending.error_type == "run_released"
    assert allocated_pending.ended_at is not None


@pytest.mark.db
async def test_close_reads_committed_run_state_over_stale_attached_run(
    db_session: AsyncSession,
    allocated_pending: Session,
    allocation_service: AllocationService,
    seeded_available_device: Device,
) -> None:
    """The session_sync sweep eager-loads ``session.run`` at sweep start. If the run is
    cancelled AFTER that load (the TR12 race), the close must re-read the run's COMMITTED
    state and stamp ``error`` — trusting the stale ``active`` object masks the cancelled
    run's session as ``passed`` (#7 outcome-masking lost-update)."""
    from sqlalchemy import update

    from app.runs.models import TestRun
    from app.sessions.service import close_running_session

    await allocation_service.confirm(db_session, allocation_id=allocated_pending.id, appium_session_id="stale-race")
    run = await create_reserved_run(
        db_session, name="stale-active-run", devices=[seeded_available_device], state=RunState.active
    )
    allocated_pending.run_id = run.id
    await db_session.flush()

    # The cancel commits out-of-band; the identity-mapped ``run`` keeps its stale ``active``
    # view — exactly the snapshot the sweep eager-loaded before the cancel committed.
    await db_session.execute(
        update(TestRun)
        .where(TestRun.id == run.id)
        .values(state=RunState.cancelled)
        .execution_options(synchronize_session=False)
    )
    assert run.state == RunState.active  # ORM view kept stale, mirroring the sweep's eager-load

    await close_running_session(db_session, allocated_pending, attached_run=run, publisher=event_bus)

    await db_session.refresh(allocated_pending)
    assert allocated_pending.status == SessionStatus.error
    assert allocated_pending.error_type == "run_released"


async def _claimed_ticket_for(db_session: AsyncSession, allocated_pending: Session) -> GridSessionQueueTicket:
    """Return the ticket that claimed ``allocated_pending`` (try_allocate set it)."""
    stmt = select(GridSessionQueueTicket).where(GridSessionQueueTicket.session_row_id == allocated_pending.id)
    ticket = (await db_session.execute(stmt)).scalars().first()
    assert ticket is not None
    assert ticket.status == GridQueueStatus.claimed
    return ticket


@pytest.mark.db
async def test_resume_claimed_returns_same_allocation_for_live_row(
    db_session: AsyncSession,
    allocated_pending: Session,
    allocation_service: AllocationService,
) -> None:
    """Lost-Allocated-response retry: a claimed ticket with a live pending row returns
    the SAME allocation, no second Session row, no second device claimed (#2)."""
    ticket = await _claimed_ticket_for(db_session, allocated_pending)
    sessions_before = len((await db_session.execute(select(Session))).scalars().all())

    result = await allocation_service.resume_claimed(db_session, ticket=ticket)

    assert result is not None
    assert result.allocation_id == allocated_pending.id
    assert ticket.status == GridQueueStatus.claimed
    sessions_after = len((await db_session.execute(select(Session))).scalars().all())
    assert sessions_after == sessions_before


@pytest.mark.db
async def test_resume_claimed_returns_same_allocation_for_running_row(
    db_session: AsyncSession,
    allocated_pending: Session,
    allocation_service: AllocationService,
) -> None:
    """A confirmed (running) row is still the honest allocation to hand back."""
    ticket = await _claimed_ticket_for(db_session, allocated_pending)
    await allocation_service.confirm(db_session, allocation_id=allocated_pending.id, appium_session_id="run-id")

    result = await allocation_service.resume_claimed(db_session, ticket=ticket)

    assert result is not None
    assert result.allocation_id == allocated_pending.id


@pytest.mark.db
async def test_resume_claimed_resets_ticket_when_row_reaped(
    db_session: AsyncSession,
    allocated_pending: Session,
    allocation_service: AllocationService,
) -> None:
    """If the claim was reaped while the response was lost, resume resets the ticket to
    waiting so the caller proceeds to a fresh try_allocate (#2)."""
    ticket = await _claimed_ticket_for(db_session, allocated_pending)
    await allocation_service.fail(db_session, allocation_id=allocated_pending.id, message="claim window expired")

    result = await allocation_service.resume_claimed(db_session, ticket=ticket)

    assert result is None
    assert ticket.status == GridQueueStatus.waiting


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

    assert reaped == {"pending_failed": 1, "tickets_expired": 1, "orphan_claims_reaped": 0}
    await db_session.refresh(allocated_pending)
    assert allocated_pending.status == SessionStatus.error
    await db_session.refresh(stale_ticket)
    assert stale_ticket.status == GridQueueStatus.expired
    await db_session.refresh(fresh_ticket)
    assert fresh_ticket.status == GridQueueStatus.waiting


@pytest.mark.db
async def test_fail_expires_claimed_ticket(
    db_session: AsyncSession,
    allocated_pending: Session,
    allocation_service: AllocationService,
    seeded_available_device: Device,
) -> None:
    """Reaper-failing a claim must terminalize the claimed ticket — otherwise it
    dangles ``claimed`` forever once the session is purged (harness G7)."""
    ticket = await _claimed_ticket_for(db_session, allocated_pending)
    await allocation_service.fail(db_session, allocation_id=allocated_pending.id, message="claim window expired")
    await db_session.refresh(ticket)
    assert ticket.status == GridQueueStatus.expired


@pytest.mark.db
async def test_mark_ended_expires_claimed_ticket(
    db_session: AsyncSession,
    allocated_pending: Session,
    allocation_service: AllocationService,
    seeded_available_device: Device,
) -> None:
    """The router-DELETE close path (mark_ended -> close_running_session) must
    terminalize the claimed ticket."""
    ticket = await _claimed_ticket_for(db_session, allocated_pending)
    await allocation_service.confirm(db_session, allocation_id=allocated_pending.id, appium_session_id="end-me")
    await allocation_service.mark_ended(db_session, appium_session_id="end-me")
    await db_session.refresh(ticket)
    assert ticket.status == GridQueueStatus.expired


@pytest.mark.db
async def test_sweep_close_expires_claimed_ticket(
    db_session: AsyncSession,
    allocated_pending: Session,
    allocation_service: AllocationService,
    seeded_available_device: Device,
) -> None:
    """The session_sync sweep close path (close_running_session directly) must
    terminalize the claimed ticket — same chokepoint as mark_ended."""
    from app.sessions.service import close_running_session

    ticket = await _claimed_ticket_for(db_session, allocated_pending)
    await allocation_service.confirm(db_session, allocation_id=allocated_pending.id, appium_session_id="swept")
    row = (
        (
            await db_session.execute(
                select(Session).options(selectinload(Session.device)).where(Session.id == allocated_pending.id)
            )
        )
        .scalars()
        .one()
    )
    await close_running_session(db_session, row, attached_run=None, publisher=event_bus)
    await db_session.refresh(ticket)
    assert ticket.status == GridQueueStatus.expired
