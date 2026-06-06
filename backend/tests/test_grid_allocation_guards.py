"""Guard branches in the grid allocation surface: unwired services, claim re-checks,
malformed older tickets, missing node targets, expired/cancelled ticket replays."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import select

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import Device, DeviceOperationalState
from app.devices.services import state_write_guard
from app.devices.services.intent import IntentService
from app.grid import router_internal
from app.grid.allocation import AllocationService, pack_slot_stereotype
from app.grid.allocation_reaper import GridAllocationReaperLoop
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.grid.services_container import GridServices
from app.sessions.models import Session, SessionStatus
from tests.helpers import seed_host_and_device, seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus
from tests.pack.factories import seed_test_packs


def _body(**caps: str) -> dict[str, Any]:
    return {"capabilities": {"alwaysMatch": caps, "firstMatch": [{}]}}


async def _stereotype_stub(db: AsyncSession, device: Device) -> dict[str, Any]:
    return {"platformName": "Android"}


def _service() -> AllocationService:
    return AllocationService(
        intent_factory=IntentService,
        publisher=event_bus,
        stereotype_provider=_stereotype_stub,
    )


@pytest_asyncio.fixture
async def seeded_available_device(db_session: AsyncSession) -> Device:
    await seed_test_packs(db_session)
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"grid-guard-{uuid.uuid4().hex[:8]}")
    return device


@pytest.mark.db
async def test_reap_expired_requires_settings(db_session: AsyncSession) -> None:
    with pytest.raises(RuntimeError, match="settings reader"):
        await _service().reap_expired(db_session)


@pytest.mark.db
async def test_reaper_cycle_requires_wired_allocation(db_session: AsyncSession) -> None:
    assert db_session.bind is not None
    from sqlalchemy.ext.asyncio import async_sessionmaker

    services = GridServices(
        settings=None,  # type: ignore[arg-type]
        session_factory=async_sessionmaker(db_session.bind),
        allocation=None,
    )
    with pytest.raises(RuntimeError, match="not wired"):
        await GridAllocationReaperLoop(services=services).run_cycle(db_session)


def test_router_allocation_helper_requires_wired_allocation() -> None:
    services = GridServices(
        settings=None,  # type: ignore[arg-type]
        session_factory=None,  # type: ignore[arg-type]
        allocation=None,
    )
    with pytest.raises(RuntimeError, match="not wired"):
        router_internal._allocation(services)


@pytest.mark.db
async def test_claim_rechecks_state_under_lock(db_session: AsyncSession, seeded_available_device: Device) -> None:
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    with state_write_guard.bypass():
        seeded_available_device.operational_state = DeviceOperationalState.maintenance
    result = await _service()._claim(
        db_session, ticket=ticket, device=seeded_available_device, candidate={}, run_id=None
    )
    assert result is None
    assert ticket.status == GridQueueStatus.waiting


@pytest.mark.db
async def test_claim_rechecks_active_sessions_under_lock(
    db_session: AsyncSession, seeded_available_device: Device
) -> None:
    db_session.add(
        Session(
            session_id=f"alloc-{uuid.uuid4()}",
            device_id=seeded_available_device.id,
            status=SessionStatus.pending,
        )
    )
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    result = await _service()._claim(
        db_session, ticket=ticket, device=seeded_available_device, candidate={}, run_id=None
    )
    assert result is None


@pytest.mark.db
async def test_claim_requires_routable_node(db_session: AsyncSession) -> None:
    # device without an AppiumNode -> no target -> claim declines
    await seed_test_packs(db_session)
    _, device = await seed_host_and_device(db_session, identity=f"grid-guard-nonode-{uuid.uuid4().hex[:8]}")
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    result = await _service()._claim(db_session, ticket=ticket, device=device, candidate={}, run_id=None)
    assert result is None


@pytest.mark.db
async def test_older_waiter_with_invalid_body_is_skipped(
    db_session: AsyncSession, seeded_available_device: Device
) -> None:
    from datetime import UTC, datetime, timedelta

    older_invalid = GridSessionQueueTicket(
        requested_body={"desiredCapabilities": {}},
        created_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    younger = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add_all([older_invalid, younger])
    await db_session.flush()
    # the malformed older ticket must not block the younger one
    assert await _service().try_allocate(db_session, ticket=younger) is not None


@pytest.mark.db
async def test_mid_restart_device_not_grid_eligible(db_session: AsyncSession, seeded_available_device: Device) -> None:
    """Node-viability predicate (#8): a device whose Appium node is mid-restart
    (transition_token set) must be excluded from grid eligibility, matching the
    run allocator's node filter."""
    from app.appium_nodes.models import AppiumNode

    node = (
        (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == seeded_available_device.id)))
        .scalars()
        .one()
    )
    # Viable before: the device is eligible.
    eligible_ids = {d.id for d in await _service()._eligible_devices(db_session)}
    assert seeded_available_device.id in eligible_ids

    with state_write_guard.bypass():
        node.transition_token = uuid.uuid4()
    await db_session.flush()

    eligible_ids = {d.id for d in await _service()._eligible_devices(db_session)}
    assert seeded_available_device.id not in eligible_ids


@pytest.mark.db
async def test_pack_slot_stereotype_tolerates_missing_pack(db_session: AsyncSession) -> None:
    # pack tables not seeded -> render_stereotype raises LookupError -> grid caps only
    _, device = await seed_host_and_device(db_session, identity=f"grid-guard-nopack-{uuid.uuid4().hex[:8]}")
    stereotype = await pack_slot_stereotype(db_session, device)
    assert stereotype.get("appium:gridfleet:deviceId") == str(device.id)
    assert "platformName" not in stereotype


@pytest.mark.db
async def test_allocate_replay_of_cancelled_ticket_is_400(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="iOS"), status=GridQueueStatus.cancelled)
    db_session.add(ticket)
    await db_session.commit()
    resp = await client.post(
        "/internal/grid/allocate", json={"body": _body(platformName="iOS"), "ticket": str(ticket.id)}
    )
    assert resp.status_code == 400
    assert resp.json()["status"] == "invalid"


@pytest.mark.db
async def test_allocate_replay_of_expired_ticket_is_410(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="iOS"), status=GridQueueStatus.expired)
    db_session.add(ticket)
    await db_session.commit()
    resp = await client.post(
        "/internal/grid/allocate", json={"body": _body(platformName="iOS"), "ticket": str(ticket.id)}
    )
    assert resp.status_code == 410
    assert resp.json()["status"] == "expired"


@pytest.mark.db
async def test_routes_skips_sessions_without_routable_device(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    # running session with no device at all
    db_session.add(Session(session_id="no-device-1", device_id=None, status=SessionStatus.running))
    # running session on a device without an AppiumNode (no target)
    _, device = await seed_host_and_device(db_session, identity=f"grid-guard-route-{uuid.uuid4().hex[:8]}")
    db_session.add(Session(session_id="no-node-1", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()
    resp = await client.get("/internal/grid/routes")
    assert resp.status_code == 200
    listed = {r["session_id"] for r in resp.json()["routes"]}
    assert "no-device-1" not in listed
    assert "no-node-1" not in listed
