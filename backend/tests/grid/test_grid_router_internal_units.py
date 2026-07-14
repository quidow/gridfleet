"""Direct-call tests for internal grid create-session handlers."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.devices.services.health import DeviceHealthService
from app.devices.services.intent import IntentService
from app.grid import router_internal, session_create
from app.grid.allocation import AllocationResult, AllocationService
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.grid.schemas_internal import ActivityRequest, CreateSessionRequest, EndedRequest
from app.grid.services_container import GridServices
from tests.conftest import settings_service
from tests.helpers import seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from app.devices.models import Device

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _body(**caps: str) -> dict[str, Any]:
    return {"capabilities": {"alwaysMatch": caps, "firstMatch": [{}]}}


async def _stereotype_stub(db: AsyncSession, device: Device, *, template_cache: object | None = None) -> dict[str, Any]:
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
    ) -> session_create.CreateOutcome:
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
    ) -> session_create.CreateOutcome:
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
    assert (await router_internal.activity(ActivityRequest(sessions=["unit-route"]), db_session)).status_code == 204
    assert (await router_internal.ended(EndedRequest(session_id="unit-route"), db_session, services)).status_code == 204
    routes = await router_internal.routes(db_session)
    assert not any(entry.session_id == "unit-route" for entry in routes.routes)

    ticket = GridSessionQueueTicket(requested_body=_body(platformName="iOS"), status=GridQueueStatus.waiting)
    db_session.add(ticket)
    await db_session.commit()
    assert (await router_internal.cancel_ticket(ticket.id, db_session)).status_code == 204
    await db_session.refresh(ticket)
    assert ticket.status == GridQueueStatus.cancelled
