"""Direct-call tests for internal grid create-session handlers."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.devices.services.health import DeviceHealthService
from app.devices.services.intent import IntentService
from app.grid import router_internal, session_create
from app.grid.allocation import AllocationResult, AllocationService
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.grid.schemas_internal import ActivityRequest, CreateSessionRequest, EndedRequest
from app.grid.services_container import GridServices
from app.sessions.models import Session
from tests.conftest import settings_service
from tests.helpers import seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from collections.abc import Collection

    from app.devices.models import Device

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


class _TxTracker:
    def __init__(self) -> None:
        self.active = 0

    def _enter(self) -> None:
        self.active += 1

    def _exit(self) -> None:
        self.active -= 1


class _TrackingCtx(AbstractAsyncContextManager[AsyncSession]):
    def __init__(self, inner: AbstractAsyncContextManager[AsyncSession], tracker: _TxTracker) -> None:
        self._inner = inner
        self._tracker = tracker

    async def __aenter__(self) -> AsyncSession:
        db = await self._inner.__aenter__()
        self._tracker._enter()
        return db

    async def __aexit__(self, *exc: object) -> bool | None:
        try:
            return await self._inner.__aexit__(*exc)
        finally:
            self._tracker._exit()


class _TrackingFactory:
    def __init__(self, inner: async_sessionmaker[AsyncSession], tracker: _TxTracker) -> None:
        self._inner = inner
        self._tracker = tracker

    def __call__(self) -> _TrackingCtx:
        return _TrackingCtx(self._inner(), self._tracker)

    def begin(self) -> _TrackingCtx:
        return _TrackingCtx(self._inner.begin(), self._tracker)


def _body(**caps: str) -> dict[str, Any]:
    return {"capabilities": {"alwaysMatch": caps, "firstMatch": [{}]}}


async def _stereotype_stub(
    db: AsyncSession, device: Device, *, template_cache: object | None = None, matching_group_keys: Collection[str] = ()
) -> dict[str, Any]:
    return {"platformName": "Android"}


@pytest.fixture(autouse=True)
def fast_long_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_internal, "LONG_POLL_SEC", 0.2)
    monkeypatch.setattr(router_internal, "RETRY_INTERVAL_SEC", 0.02)


@pytest.fixture
def services(db_session: AsyncSession) -> GridServices:
    assert db_session.bind is not None
    allocation = AllocationService(
        intent_factory=IntentService,
        publisher=event_bus,
        stereotype_provider=_stereotype_stub,
        settings=settings_service,
    )
    return GridServices(
        settings=settings_service,
        session_factory=async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False),
        allocation=allocation,
        health=DeviceHealthService(publisher=event_bus),
    )


@pytest_asyncio.fixture
async def seeded_available_device(db_session: AsyncSession) -> Device:
    await seed_test_packs(db_session)
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"grid-unit-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    return device


@pytest.mark.db
async def test_create_session_handler_claims_then_creates(
    services: GridServices, seeded_available_device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_create(
        db_factory: session_create.DbFactory,
        allocation_service: AllocationService,
        *,
        allocation: AllocationResult,
        raw_body: bytes,
        claim_window_sec: int,
        max_create_timeout_sec: float | None = None,
    ) -> session_create.CreateOutcome:
        _ = max_create_timeout_sec
        return session_create.CreateOutcome(
            kind="created",
            session_id="unit-session",
            appium_status=200,
            appium_body={"value": {"sessionId": "unit-session"}},
            allocation=allocation,
        )

    monkeypatch.setattr(router_internal.session_create, "create_and_promote", fake_create)
    resp = await router_internal.create_session(CreateSessionRequest(body=_body(platformName="Android")), services)
    assert resp.status == "created"
    assert resp.session_id == "unit-session"
    assert resp.target is not None


@pytest.mark.db
async def test_create_session_handler_queued_then_reuses_ticket(
    services: GridServices, seeded_available_device: Device
) -> None:
    queued = await router_internal.create_session(CreateSessionRequest(body=_body(platformName="iOS")), services)
    assert queued.status == "queued"
    assert queued.ticket is not None
    queued_again = await router_internal.create_session(
        CreateSessionRequest(body=_body(platformName="iOS"), ticket=queued.ticket), services
    )
    assert queued_again.status == "queued"
    assert queued_again.ticket == queued.ticket


@pytest.mark.db
async def test_create_session_handler_cancelled_and_expired_ticket_are_terminal(
    services: GridServices, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    cancelled = GridSessionQueueTicket(requested_body=_body(platformName="iOS"), status=GridQueueStatus.cancelled)
    expired = GridSessionQueueTicket(requested_body=_body(platformName="iOS"), status=GridQueueStatus.expired)
    db_session.add_all([cancelled, expired])
    await db_session.commit()
    invalid = await router_internal.create_session(
        CreateSessionRequest(body=_body(platformName="iOS"), ticket=cancelled.id), services
    )
    timed_out = await router_internal.create_session(
        CreateSessionRequest(body=_body(platformName="iOS"), ticket=expired.id), services
    )
    assert invalid.status_code == 400
    assert timed_out.status_code == 410


@pytest.mark.db
async def test_cancel_and_lifecycle_handlers(
    services: GridServices,
    db_session: AsyncSession,
    seeded_available_device: Device,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create(
        db_factory: session_create.DbFactory,
        allocation_service: AllocationService,
        *,
        allocation: AllocationResult,
        raw_body: bytes,
        claim_window_sec: int,
        max_create_timeout_sec: float | None = None,
    ) -> session_create.CreateOutcome:
        _ = max_create_timeout_sec
        async with db_factory() as db:
            await allocation_service.promote_to_running(
                db, allocation_id=allocation.allocation_id, appium_session_id="unit-route"
            )
            await db.commit()
        return session_create.CreateOutcome(
            kind="created",
            session_id="unit-route",
            appium_status=200,
            appium_body={"value": {"sessionId": "unit-route"}},
            allocation=allocation,
        )

    monkeypatch.setattr(router_internal.session_create, "create_and_promote", fake_create)
    created = await router_internal.create_session(CreateSessionRequest(body=_body(platformName="Android")), services)
    assert created.status == "created"
    routes = await router_internal.routes(db_session)
    assert any(entry.session_id == "unit-route" for entry in routes.routes)
    assert (await router_internal.activity(ActivityRequest(sessions=["unit-route"]), services)).status_code == 204
    assert (await router_internal.ended(EndedRequest(session_id="unit-route"), services)).status_code == 204
    routes = await router_internal.routes(db_session)
    assert not any(entry.session_id == "unit-route" for entry in routes.routes)

    ticket = GridSessionQueueTicket(requested_body=_body(platformName="iOS"), status=GridQueueStatus.waiting)
    db_session.add(ticket)
    await db_session.commit()
    assert (await router_internal.cancel_ticket(ticket.id, services)).status_code == 204
    await db_session.refresh(ticket)
    assert ticket.status == GridQueueStatus.cancelled


@pytest_asyncio.fixture
async def two_available_devices(db_session: AsyncSession) -> list[Device]:
    await seed_test_packs(db_session)
    _, dev_a, _ = await seed_host_and_running_node(db_session, identity=f"race-a-{uuid.uuid4().hex[:8]}")
    _, dev_b, _ = await seed_host_and_running_node(db_session, identity=f"race-b-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    return [dev_a, dev_b]


@pytest.mark.db
async def test_same_ticket_race_produces_at_most_one_live_session(
    services: GridServices,
    two_available_devices: list[Device],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent create-session polls carrying the same ticket id must not
    each claim a device: the queue-ticket root lock serializes them so at most
    one live Session row carries that ticket id."""
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.commit()
    ticket_id = ticket.id

    # Fake create_and_promote so the handler returns immediately after a claim,
    # leaving the pending row in place (ended_at is None -> counts as live).
    async def fake_create(
        db_factory: session_create.DbFactory,
        allocation_service: AllocationService,
        *,
        allocation: AllocationResult,
        raw_body: bytes,
        claim_window_sec: int,
        max_create_timeout_sec: float | None = None,
    ) -> session_create.CreateOutcome:
        _ = db_factory, allocation_service, raw_body, claim_window_sec, max_create_timeout_sec
        return session_create.CreateOutcome(
            kind="created",
            session_id=f"race-{allocation.allocation_id}",
            appium_status=200,
            appium_body={"value": {"sessionId": f"race-{allocation.allocation_id}"}},
            allocation=allocation,
        )

    # Slow try_allocate so both polls overlap inside the allocation step (in the
    # pre-fix code there is no ticket lock, so both reuse the ticket row).
    real_try_allocate = services.allocation.try_allocate

    async def slow_try_allocate(
        db: AsyncSession,
        *,
        ticket: GridSessionQueueTicket,
        exclude_device_ids: set[uuid.UUID] | None = None,
    ) -> AllocationResult | None:
        await asyncio.sleep(0.05)
        return await real_try_allocate(db, ticket=ticket, exclude_device_ids=exclude_device_ids)

    monkeypatch.setattr(router_internal.session_create, "create_and_promote", fake_create)
    monkeypatch.setattr(services.allocation, "try_allocate", slow_try_allocate)

    payload = CreateSessionRequest(body=_body(platformName="Android"), ticket=ticket_id)
    await asyncio.gather(
        router_internal.create_session(payload, services),
        router_internal.create_session(payload, services),
    )

    async with services.session_factory() as db:
        live = (
            (await db.execute(select(Session).where(Session.ticket_id == ticket_id, Session.ended_at.is_(None))))
            .scalars()
            .all()
        )
    assert len(live) <= 1, f"same ticket produced {len(live)} live sessions"


@pytest.mark.db
async def test_resume_interrupted_terminates_appium_with_no_open_transaction(
    services: GridServices,
    seeded_available_device: Device,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted create with a live running session must terminate the
    remote Appium session OUTSIDE any database transaction (prepare/effect/finalize)."""
    tracker = _TxTracker()
    tracked_factory = _TrackingFactory(services.session_factory, tracker)
    tracked_services = GridServices(
        settings=services.settings,
        session_factory=tracked_factory,
        allocation=services.allocation,
        health=services.health,
    )

    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    result = await services.allocation.try_allocate(db_session, ticket=ticket)
    assert result is not None
    await services.allocation.promote_to_running(
        db_session, allocation_id=result.allocation_id, appium_session_id="interrupted-ssn"
    )
    await db_session.commit()

    terminated: list[str] = []

    async def fake_terminate(target: str, session_id: str, *, timeout: float = 10.0) -> bool:
        assert tracker.active == 0, "Appium terminate issued with an open transaction"
        terminated.append(session_id)
        return True

    monkeypatch.setattr(router_internal.appium_direct, "terminate_session", fake_terminate)

    payload = CreateSessionRequest(body=_body(platformName="Android"), ticket=ticket.id)
    resp = await router_internal.create_session(payload, tracked_services)
    # The interrupted row is ended; the handler proceeds to a fresh claim which
    # either allocates the now-free device or queues. Either way termination ran
    # with no open transaction.
    assert terminated == ["interrupted-ssn"]
    assert resp.status in {"created", "queued"}
