"""Direct-call unit tests for the internal grid allocation handlers.

The handlers are async route functions whose bodies are only otherwise exercised
through the in-process ``client`` fixture, where coverage of the long-poll loop is
timing-sensitive under xdist. Calling the handlers directly with a real
session_factory makes every branch (queued/expired/cancelled/allocated/resume)
deterministically traced.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from app.devices.models import Device

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.devices.services.intent import IntentService
from app.grid import router_internal
from app.grid.allocation import AllocationService
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.grid.schemas_internal import (
    ActivityRequest,
    AllocateRequest,
    ConfirmRequest,
    EndedRequest,
    FailRequest,
)
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
    )


@pytest_asyncio.fixture
async def seeded_available_device(db_session: AsyncSession) -> Device:
    await seed_test_packs(db_session)
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"rinternal-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    return device


@pytest.mark.db
async def test_allocate_handler_allocated(services: GridServices, seeded_available_device: Device) -> None:
    resp = await router_internal.allocate(AllocateRequest(body=_body(platformName="Android")), services)
    assert resp.status == "allocated"
    assert resp.target and resp.target.startswith("http://")
    assert resp.claim_window_sec == 120


@pytest.mark.db
async def test_allocate_handler_queued_then_resume(services: GridServices, seeded_available_device: Device) -> None:
    # No match -> queued with a ticket.
    queued = await router_internal.allocate(AllocateRequest(body=_body(platformName="iOS")), services)
    assert queued.status == "queued"
    assert queued.ticket is not None
    # Re-poll with the same ticket -> still queued (the long-poll loop re-attempts).
    queued2 = await router_internal.allocate(
        AllocateRequest(body=_body(platformName="iOS"), ticket=queued.ticket), services
    )
    assert queued2.status == "queued"
    assert queued2.ticket == queued.ticket


@pytest.mark.db
async def test_allocate_handler_cancelled_ticket_is_400(
    services: GridServices, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="iOS"), status=GridQueueStatus.cancelled)
    db_session.add(ticket)
    await db_session.commit()
    resp = await router_internal.allocate(AllocateRequest(body=_body(platformName="iOS"), ticket=ticket.id), services)
    # JSONResponse for the invalid path.
    assert resp.status_code == 400


@pytest.mark.db
async def test_allocate_handler_expired_ticket_is_410(
    services: GridServices, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="iOS"), status=GridQueueStatus.expired)
    db_session.add(ticket)
    await db_session.commit()
    resp = await router_internal.allocate(AllocateRequest(body=_body(platformName="iOS"), ticket=ticket.id), services)
    assert resp.status_code == 410


@pytest.mark.db
async def test_allocate_handler_invalid_body_is_400(services: GridServices, seeded_available_device: Device) -> None:
    resp = await router_internal.allocate(
        AllocateRequest(body={"desiredCapabilities": {"platformName": "Android"}}), services
    )
    assert resp.status_code == 400


@pytest.mark.db
async def test_confirm_fail_ended_activity_routes_handlers(
    services: GridServices, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    alloc = await router_internal.allocate(AllocateRequest(body=_body(platformName="Android")), services)
    allocation_id = alloc.allocation_id
    assert allocation_id is not None

    confirmed = await router_internal.confirm(
        allocation_id, ConfirmRequest(appium_session_id="appium-x"), db_session, services
    )
    assert confirmed.status_code == 204

    routes = await router_internal.routes(db_session)
    assert any(r.session_id == "appium-x" for r in routes.routes)

    activity = await router_internal.activity(
        ActivityRequest(sessions={"appium-x": "2026-06-06T00:00:00Z"}), db_session
    )
    assert activity.status_code == 204

    ended = await router_internal.ended(EndedRequest(session_id="appium-x"), db_session, services)
    assert ended.status_code == 204


@pytest.mark.db
async def test_confirm_unknown_allocation_is_409(services: GridServices, db_session: AsyncSession) -> None:
    resp = await router_internal.confirm(uuid.uuid4(), ConfirmRequest(appium_session_id="nope"), db_session, services)
    assert resp.status_code == 409


@pytest.mark.db
async def test_fail_handler(services: GridServices, db_session: AsyncSession, seeded_available_device: Device) -> None:
    alloc = await router_internal.allocate(AllocateRequest(body=_body(platformName="Android")), services)
    assert alloc.allocation_id is not None
    resp = await router_internal.fail(alloc.allocation_id, FailRequest(message="boom"), db_session, services)
    assert resp.status_code == 204
    row = await db_session.get(Session, alloc.allocation_id)
    assert row is not None
    await db_session.refresh(row)
    assert row.status == SessionStatus.error


@pytest.mark.db
async def test_cancel_ticket_handler(services: GridServices, db_session: AsyncSession) -> None:
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="iOS"), status=GridQueueStatus.waiting)
    db_session.add(ticket)
    await db_session.commit()
    resp = await router_internal.cancel_ticket(ticket.id, db_session)
    assert resp.status_code == 204
    await db_session.refresh(ticket)
    assert ticket.status == GridQueueStatus.cancelled


@pytest.mark.db
async def test_activity_empty_is_noop(db_session: AsyncSession) -> None:
    resp = await router_internal.activity(ActivityRequest(sessions={}), db_session)
    assert resp.status_code == 204


@pytest.mark.db
async def test_allocate_handler_resumes_claimed_ticket(
    services: GridServices, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    """A retry carrying an already-claimed ticket returns the SAME allocation via the
    resume-claimed branch of the allocate handler."""
    alloc = await router_internal.allocate(AllocateRequest(body=_body(platformName="Android")), services)
    assert alloc.allocation_id is not None
    # Find the claimed ticket that the allocation produced.
    from sqlalchemy import select

    ticket = (
        (
            await db_session.execute(
                select(GridSessionQueueTicket).where(GridSessionQueueTicket.session_row_id == alloc.allocation_id)
            )
        )
        .scalars()
        .first()
    )
    assert ticket is not None and ticket.status == GridQueueStatus.claimed

    resumed = await router_internal.allocate(
        AllocateRequest(body=_body(platformName="Android"), ticket=ticket.id), services
    )
    assert resumed.status == "allocated"
    assert resumed.allocation_id == alloc.allocation_id
