"""AllocationService.try_allocate: match -> pending row + busy, FIFO fairness, reservation gate."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unittest.mock import AsyncMock

from app.devices.models import Device, DeviceOperationalState, DeviceReservation
from app.devices.services.intent import IntentService
from app.grid.allocation import AllocationService
from app.grid.matching import RUN_ID_CAP
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.runs.models import RunState, TestRun
from app.sessions.models import Session, SessionStatus
from app.sessions.service import SessionCrudService
from tests.helpers import drain_handlers, recent_events, seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus


def _body(**caps: str) -> dict[str, Any]:
    return {"capabilities": {"alwaysMatch": caps, "firstMatch": [{}]}}


async def _stereotype_stub(db: AsyncSession, device: Device, *, template_cache: object | None = None) -> dict[str, Any]:
    return {
        "platformName": "Android",
        "appium:udid": device.connection_target,
        "appium:gridfleet:deviceId": str(device.id),
    }


def _make_service(db: AsyncSession) -> AllocationService:
    return AllocationService(
        intent_factory=IntentService,
        publisher=event_bus,
        stereotype_provider=_stereotype_stub,
    )


@pytest_asyncio.fixture
async def seeded_available_device(db_session: AsyncSession) -> Device:
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"grid-alloc-{uuid.uuid4().hex[:8]}")
    return device


@dataclass
class ReservedDevice:
    device: Device
    reservation_run_id: uuid.UUID


@pytest_asyncio.fixture
async def seeded_reserved_device(db_session: AsyncSession) -> ReservedDevice:
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"grid-alloc-res-{uuid.uuid4().hex[:8]}")
    run = TestRun(
        id=uuid.uuid4(),
        name="grid-alloc-reserved-run",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        DeviceReservation(
            run_id=run.id,
            device_id=device.id,
            identity_value=device.identity_value,
            connection_target=device.connection_target,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            os_version=device.os_version,
        )
    )
    await db_session.commit()
    return ReservedDevice(device=device, reservation_run_id=run.id)


@pytest.fixture
def allocation_service(db_session: AsyncSession) -> AllocationService:
    return _make_service(db_session)


@pytest.mark.db
async def test_allocate_creates_pending_and_busy(
    db_session: AsyncSession, seeded_available_device: Device, allocation_service: AllocationService
) -> None:
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    result = await allocation_service.try_allocate(db_session, ticket=ticket)
    assert result is not None
    assert result.target.startswith("http://") and result.target.endswith(":4730")
    row = await db_session.get(Session, result.allocation_id)
    assert row is not None
    assert row.status == SessionStatus.pending
    # #6: the allocation target is persisted on the row so /routes can fall back to it.
    assert row.router_target == result.target
    assert ticket.status == GridQueueStatus.claimed
    assert ticket.session_row_id == row.id
    await db_session.refresh(seeded_available_device)
    assert seeded_available_device.operational_state == DeviceOperationalState.busy


@pytest.mark.db
async def test_no_match_leaves_ticket_waiting(
    db_session: AsyncSession, seeded_available_device: Device, allocation_service: AllocationService
) -> None:
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="iOS"))
    db_session.add(ticket)
    await db_session.flush()
    assert await allocation_service.try_allocate(db_session, ticket=ticket) is None
    assert ticket.status == GridQueueStatus.waiting


@pytest.mark.db
async def test_invalid_body_cancels_ticket(
    db_session: AsyncSession, seeded_available_device: Device, allocation_service: AllocationService
) -> None:
    ticket = GridSessionQueueTicket(requested_body={"desiredCapabilities": {"platformName": "Android"}})
    db_session.add(ticket)
    await db_session.flush()
    assert await allocation_service.try_allocate(db_session, ticket=ticket) is None
    assert ticket.status == GridQueueStatus.cancelled


@pytest.mark.db
async def test_reserved_device_requires_run_id(
    db_session: AsyncSession, seeded_reserved_device: ReservedDevice, allocation_service: AllocationService
) -> None:
    # without run id -> no match
    t1 = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(t1)
    await db_session.flush()
    assert await allocation_service.try_allocate(db_session, ticket=t1) is None
    # with the reservation's run id -> match
    t2 = GridSessionQueueTicket(
        requested_body=_body(platformName="Android", **{RUN_ID_CAP: str(seeded_reserved_device.reservation_run_id)}),
        created_at=datetime.now(UTC) - timedelta(seconds=10),  # older than t1 so FIFO fairness cannot block it
    )
    db_session.add(t2)
    await db_session.flush()
    result = await allocation_service.try_allocate(db_session, ticket=t2)
    assert result is not None
    row = await db_session.get(Session, result.allocation_id)
    assert row is not None
    assert row.run_id == seeded_reserved_device.reservation_run_id


@pytest.mark.db
async def test_fifo_fairness_blocks_younger_ticket(
    db_session: AsyncSession, seeded_available_device: Device, allocation_service: AllocationService
) -> None:
    now = datetime.now(UTC)
    older = GridSessionQueueTicket(requested_body=_body(platformName="Android"), created_at=now - timedelta(seconds=5))
    younger = GridSessionQueueTicket(requested_body=_body(platformName="Android"), created_at=now)
    db_session.add_all([older, younger])
    await db_session.flush()
    assert await allocation_service.try_allocate(db_session, ticket=younger) is None
    assert await allocation_service.try_allocate(db_session, ticket=older) is not None


@pytest.mark.db
async def test_reserved_device_younger_run_ticket_not_blocked_by_older_runless(
    db_session: AsyncSession, seeded_reserved_device: ReservedDevice, allocation_service: AllocationService
) -> None:
    """Reservation-aware FIFO veto (#5): an OLDER run-less waiter that the
    reservation gate would reject for the reserved device must NOT block the run's
    own YOUNGER ticket — the younger run-owned ticket allocates immediately."""
    now = datetime.now(UTC)
    older_runless = GridSessionQueueTicket(
        requested_body=_body(platformName="Android"),
        created_at=now - timedelta(seconds=10),
    )
    younger_run_owned = GridSessionQueueTicket(
        requested_body=_body(platformName="Android", **{RUN_ID_CAP: str(seeded_reserved_device.reservation_run_id)}),
        created_at=now,
    )
    db_session.add_all([older_runless, younger_run_owned])
    await db_session.flush()

    # The older run-less ticket cannot take the reserved device, so it must not veto.
    result = await allocation_service.try_allocate(db_session, ticket=younger_run_owned)
    assert result is not None
    row = await db_session.get(Session, result.allocation_id)
    assert row is not None
    assert row.run_id == seeded_reserved_device.reservation_run_id
    # The older run-less ticket still cannot allocate the reserved device.
    assert await allocation_service.try_allocate(db_session, ticket=older_runless) is None


@pytest.mark.db
async def test_concurrent_allocation_single_winner(
    db_session_maker: async_sessionmaker[AsyncSession], seeded_available_device: Device
) -> None:
    async def attempt() -> bool:
        async with db_session_maker() as db:
            ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
            db.add(ticket)
            await db.flush()
            result = await _make_service(db).try_allocate(db, ticket=ticket)
            await db.commit()
            return result is not None

    outcomes = await asyncio.gather(attempt(), attempt())
    # With two independent transactions the FIFO check may make either ticket
    # lose — the invariant is exactly-one-winner, not which.
    assert sorted(outcomes) == [False, True]


async def _started_events_for(session_id: str) -> list[dict[str, Any]]:
    """Drain after-commit handlers and return session.started events for a session id."""
    await drain_handlers(event_bus)
    return [
        e["data"]
        for e in recent_events(event_bus, event_types=["session.started"])
        if e["data"].get("session_id") == session_id
    ]


@pytest.mark.db
async def test_confirm_emits_one_session_started_for_free_row(
    db_session: AsyncSession, seeded_available_device: Device, allocation_service: AllocationService
) -> None:
    """confirm() is the authoritative emission point: a run-less allocation emits
    exactly one session.started with no run id (spec §8)."""
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    result = await allocation_service.try_allocate(db_session, ticket=ticket)
    assert result is not None
    await allocation_service.confirm(db_session, allocation_id=result.allocation_id, appium_session_id="free-ssn")
    await db_session.commit()

    started = await _started_events_for("free-ssn")
    assert len(started) == 1
    assert started[0]["run_id"] is None


@pytest.mark.db
async def test_confirm_emits_one_session_started_with_run_id(
    db_session: AsyncSession, seeded_reserved_device: ReservedDevice, allocation_service: AllocationService
) -> None:
    """A run-bound allocation emits exactly one session.started carrying the run id,
    proving consumers can attribute router sessions to their reservation (spec §8)."""
    ticket = GridSessionQueueTicket(
        requested_body=_body(platformName="Android", **{RUN_ID_CAP: str(seeded_reserved_device.reservation_run_id)}),
    )
    db_session.add(ticket)
    await db_session.flush()
    result = await allocation_service.try_allocate(db_session, ticket=ticket)
    assert result is not None
    await allocation_service.confirm(db_session, allocation_id=result.allocation_id, appium_session_id="run-ssn")
    await db_session.commit()

    started = await _started_events_for("run-ssn")
    assert len(started) == 1
    assert started[0]["run_id"] == str(seeded_reserved_device.reservation_run_id)


@pytest.mark.db
async def test_allocate_confirm_register_emits_session_started_once(
    db_session: AsyncSession, seeded_available_device: Device, allocation_service: AllocationService
) -> None:
    """De-dup: the full allocate -> confirm -> legacy register flow yields exactly ONE
    session.started. confirm() emits; register_session then finds the already-running
    row and returns early without a second emission."""
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    result = await allocation_service.try_allocate(db_session, ticket=ticket)
    assert result is not None
    await allocation_service.confirm(db_session, allocation_id=result.allocation_id, appium_session_id="dedup-ssn")
    await db_session.commit()

    crud = SessionCrudService(publisher=event_bus, lifecycle=AsyncMock())
    await crud.register_session(
        db_session,
        session_id="dedup-ssn",
        test_name="dedup",
        device_id=seeded_available_device.id,
        status=SessionStatus.running,
    )

    started = await _started_events_for("dedup-ssn")
    assert len(started) == 1
