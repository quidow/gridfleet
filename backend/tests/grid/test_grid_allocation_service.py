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


from app.devices.models import Device, DeviceOperationalState, DeviceReservation
from app.devices.services.intent import IntentService
from app.grid.allocation import AllocationNotPendingError, AllocationService, RunNotActiveError
from app.grid.matching import CapabilityMergeError
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.runs.models import RunState, TestRun
from app.sessions.models import Session, SessionStatus
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
async def test_eligible_devices_sets_gauge(
    db_session: AsyncSession, seeded_available_device: Device, allocation_service: AllocationService
) -> None:
    from app.grid.allocation import GRID_ELIGIBLE_DEVICES

    eligible = await allocation_service._eligible_devices(db_session)
    assert seeded_available_device.id in {d.id for d in eligible}
    assert GRID_ELIGIBLE_DEVICES._value.get() == len(eligible)  # type: ignore[attr-defined]


@pytest.mark.db
async def test_allocate_extracts_test_name_from_capabilities(
    db_session: AsyncSession, seeded_available_device: Device, allocation_service: AllocationService
) -> None:
    """The client's ``gridfleet:testName`` cap must land in the Session.test_name column so the
    Sessions UI shows it. The router/grid flow that replaced the legacy register_session API
    dropped this extraction, leaving every grid session's TEST NAME blank."""
    body = {
        "capabilities": {
            "alwaysMatch": {"platformName": "Android", "gridfleet:testName": "state-test"},
            "firstMatch": [{}],
        }
    }
    ticket = GridSessionQueueTicket(requested_body=body)
    db_session.add(ticket)
    await db_session.flush()
    result = await allocation_service.try_allocate(db_session, ticket=ticket)
    assert result is not None
    row = await db_session.get(Session, result.allocation_id)
    assert row is not None
    assert row.test_name == "state-test"


@pytest.mark.db
async def test_allocate_without_test_name_leaves_it_null(
    db_session: AsyncSession, seeded_available_device: Device, allocation_service: AllocationService
) -> None:
    """No ``gridfleet:testName`` cap → test_name stays NULL (the UI shows ``-``)."""
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    result = await allocation_service.try_allocate(db_session, ticket=ticket)
    assert result is not None
    row = await db_session.get(Session, result.allocation_id)
    assert row is not None
    assert row.test_name is None


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
    # The merge error re-raises after cancelling so the API layer can surface the
    # descriptive message in the 400 body (wave-5 #26).
    with pytest.raises(CapabilityMergeError, match="capabilities"):
        await allocation_service.try_allocate(db_session, ticket=ticket)
    assert ticket.status == GridQueueStatus.cancelled


@pytest.mark.db
async def test_reserved_device_admits_only_its_run_ticket(
    db_session: AsyncSession, seeded_reserved_device: ReservedDevice, allocation_service: AllocationService
) -> None:
    # Free ticket (no run binding) must NOT take a reserved device.
    free_ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(free_ticket)
    await db_session.flush()
    assert await allocation_service.try_allocate(db_session, ticket=free_ticket) is None

    # Ticket bound to the reservation's run -> match, session joins the run.
    bound = GridSessionQueueTicket(
        requested_body=_body(platformName="Android"),
        run_id=seeded_reserved_device.reservation_run_id,
    )
    db_session.add(bound)
    await db_session.flush()
    result = await allocation_service.try_allocate(db_session, ticket=bound)
    assert result is not None
    row = await db_session.get(Session, result.allocation_id)
    assert row is not None
    assert row.run_id == seeded_reserved_device.reservation_run_id


@pytest.mark.db
async def test_run_bound_ticket_rejected_on_unreserved_device(
    db_session: AsyncSession, seeded_available_device: Device, allocation_service: AllocationService
) -> None:
    """Spec §1: no spillover — a run ticket never lands on an unreserved device."""
    run = TestRun(
        id=uuid.uuid4(),
        name="strict-no-spill-run",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.flush()
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"), run_id=run.id)
    db_session.add(ticket)
    await db_session.flush()
    assert await allocation_service.try_allocate(db_session, ticket=ticket) is None
    assert ticket.status == GridQueueStatus.waiting  # keeps waiting for its reservations


@pytest.mark.db
async def test_wrong_run_ticket_rejected_on_reserved_device(
    db_session: AsyncSession, seeded_reserved_device: ReservedDevice, allocation_service: AllocationService
) -> None:
    other_run = TestRun(
        id=uuid.uuid4(),
        name="strict-wrong-run",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=datetime.now(UTC),
    )
    db_session.add(other_run)
    await db_session.flush()
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"), run_id=other_run.id)
    db_session.add(ticket)
    await db_session.flush()
    assert await allocation_service.try_allocate(db_session, ticket=ticket) is None


@pytest.mark.db
async def test_run_not_active_cancels_ticket(
    db_session: AsyncSession, seeded_available_device: Device, allocation_service: AllocationService
) -> None:
    """A vanished/ended run fails the waiter NOW with a clear error, not at queue timeout."""
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"), run_id=uuid.uuid4())
    db_session.add(ticket)
    await db_session.flush()
    with pytest.raises(RunNotActiveError, match="is missing"):
        await allocation_service.try_allocate(db_session, ticket=ticket)
    assert ticket.status == GridQueueStatus.cancelled


async def _seed_reserved_device_for_run(db_session: AsyncSession, *, state: RunState) -> ReservedDevice:
    """Like the seeded_reserved_device fixture but with a caller-chosen run state."""
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"grid-alloc-prep-{uuid.uuid4().hex[:8]}")
    run = TestRun(
        id=uuid.uuid4(),
        name="grid-alloc-preparing-run",
        state=state,
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


@pytest.mark.db
async def test_preparing_run_ticket_allocates_its_reserved_device(
    db_session: AsyncSession, allocation_service: AllocationService
) -> None:
    """Preparation sessions are legit (docs: runs-and-reservations.md §preparing): a run-bound
    ticket whose run is still `preparing` allocates ITS reserved device and links the session."""
    reserved = await _seed_reserved_device_for_run(db_session, state=RunState.preparing)
    ticket = GridSessionQueueTicket(
        requested_body=_body(platformName="Android"),
        run_id=reserved.reservation_run_id,
    )
    db_session.add(ticket)
    await db_session.flush()

    result = await allocation_service.try_allocate(db_session, ticket=ticket)
    assert result is not None
    row = await db_session.get(Session, result.allocation_id)
    assert row is not None
    assert row.run_id == reserved.reservation_run_id


@pytest.mark.db
async def test_preparing_run_reservation_blocks_free_ticket(
    db_session: AsyncSession, allocation_service: AllocationService
) -> None:
    """A device reserved for a `preparing` run is protected: a free ticket cannot steal it
    during the preparation window (Option A — reservations gate from creation, not just active)."""
    await _seed_reserved_device_for_run(db_session, state=RunState.preparing)
    free_ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(free_ticket)
    await db_session.flush()

    assert await allocation_service.try_allocate(db_session, ticket=free_ticket) is None


@pytest.mark.db
async def test_legacy_run_id_cap_rejected(
    db_session: AsyncSession, seeded_available_device: Device, allocation_service: AllocationService
) -> None:
    """Clean-break tombstone: cap-era clients get a loud, actionable rejection."""
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android", **{"gridfleet:run_id": "free"}))
    db_session.add(ticket)
    await db_session.flush()
    with pytest.raises(CapabilityMergeError, match="no longer supported"):
        await allocation_service.try_allocate(db_session, ticket=ticket)
    assert ticket.status == GridQueueStatus.cancelled


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
        requested_body=_body(platformName="Android"),
        run_id=seeded_reserved_device.reservation_run_id,
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
        requested_body=_body(platformName="Android"),
        run_id=seeded_reserved_device.reservation_run_id,
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
async def test_confirm_conflicting_running_row_raises_not_pending_not_500(
    db_session: AsyncSession, seeded_available_device: Device, allocation_service: AllocationService
) -> None:
    """C5: a running(X) row already carrying the Appium session id (e.g. inserted by
    the legacy register API while the alloc row still held its placeholder) must make
    confirm() raise AllocationNotPendingError (router -> 409 rollback), NOT an
    unhandled IntegrityError 500 that wedges the allocation."""
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    result = await allocation_service.try_allocate(db_session, ticket=ticket)
    assert result is not None

    # A DIFFERENT running session already owns the Appium id (the partial unique index
    # ux_sessions_session_id_running will reject confirm's UPDATE).
    db_session.add(
        Session(
            id=uuid.uuid4(),
            session_id="conflict-ssn",
            device_id=None,
            status=SessionStatus.running,
        )
    )
    await db_session.flush()

    with pytest.raises(AllocationNotPendingError):
        await allocation_service.confirm(
            db_session, allocation_id=result.allocation_id, appium_session_id="conflict-ssn"
        )


@pytest.mark.db
async def test_older_run_waiter_does_not_veto_free_ticket_on_unreserved_device(
    db_session: AsyncSession, seeded_available_device: Device, allocation_service: AllocationService
) -> None:
    """A run-bound older waiter can never take an unreserved device, so it must not veto."""
    run = TestRun(
        id=uuid.uuid4(),
        name="fifo-run-waiter",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.flush()
    older = GridSessionQueueTicket(requested_body=_body(platformName="Android"), run_id=run.id)
    db_session.add(older)
    await db_session.flush()
    older.last_polled_at = datetime.now(UTC)  # live waiter
    younger = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(younger)
    await db_session.flush()
    result = await allocation_service.try_allocate(db_session, ticket=younger)
    assert result is not None


@pytest.mark.db
async def test_stale_older_ticket_does_not_veto_younger_live_waiter(
    db_session: AsyncSession, seeded_available_device: Device, allocation_service: AllocationService
) -> None:
    """C8: an older waiting ticket whose client stopped polling (stale last_polled_at)
    must NOT FIFO-veto a younger live waiter — otherwise one dead client starves the
    queue for up to grid.queue_timeout_sec."""
    now = datetime.now(UTC)
    dead_older = GridSessionQueueTicket(
        requested_body=_body(platformName="Android"),
        created_at=now - timedelta(seconds=30),
        last_polled_at=now - timedelta(seconds=60),  # client gone (>> 10x1s window)
    )
    live_younger = GridSessionQueueTicket(
        requested_body=_body(platformName="Android"),
        created_at=now,
    )
    db_session.add_all([dead_older, live_younger])
    await db_session.flush()

    # The dead older ticket is presumed dead and cannot block; the younger allocates.
    result = await allocation_service.try_allocate(db_session, ticket=live_younger)
    assert result is not None
