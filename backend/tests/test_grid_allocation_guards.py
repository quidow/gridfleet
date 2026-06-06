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
from app.grid.allocation import AllocationService, pack_slot_stereotype
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
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
async def test_claim_declines_when_node_not_viable_under_lock(
    db_session: AsyncSession, seeded_available_device: Device
) -> None:
    """_claim re-checks node viability under the row lock: a node mid-restart
    (transition_token set) after _eligible_devices ran is declined."""
    from app.appium_nodes.models import AppiumNode

    node = (
        (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == seeded_available_device.id)))
        .scalars()
        .one()
    )
    with state_write_guard.bypass():
        node.transition_token = uuid.uuid4()
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    result = await _service()._claim(
        db_session, ticket=ticket, device=seeded_available_device, candidate={}, run_id=None
    )
    assert result is None


@pytest.mark.db
async def test_resume_claimed_without_session_row_resets_to_waiting(db_session: AsyncSession) -> None:
    """A claimed ticket with no session_row_id (never reached _claim's flush) resets to
    waiting instead of resuming."""
    ticket = GridSessionQueueTicket(
        requested_body=_body(platformName="Android"),
        status=GridQueueStatus.claimed,
        session_row_id=None,
    )
    db_session.add(ticket)
    await db_session.flush()
    result = await _service().resume_claimed(db_session, ticket=ticket)
    assert result is None
    assert ticket.status == GridQueueStatus.waiting


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
    # running session with no device at all and no stored target
    db_session.add(Session(session_id="no-device-1", device_id=None, status=SessionStatus.running))
    # running session on a device without an AppiumNode (no live target, no stored target)
    _, device = await seed_host_and_device(db_session, identity=f"grid-guard-route-{uuid.uuid4().hex[:8]}")
    db_session.add(Session(session_id="no-node-1", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()
    resp = await client.get("/internal/grid/routes")
    assert resp.status_code == 200
    listed = {r["session_id"] for r in resp.json()["routes"]}
    assert "no-device-1" not in listed
    assert "no-node-1" not in listed


@pytest.mark.db
async def test_routes_uses_live_target_when_available(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    # Live node target present: it wins even when a (stale) stored target also exists.
    db_session.add(
        Session(
            session_id="live-1",
            device_id=seeded_available_device.id,
            status=SessionStatus.running,
            router_target="http://stale:9999",
        )
    )
    await db_session.commit()
    resp = await client.get("/internal/grid/routes")
    assert resp.status_code == 200
    routes = {r["session_id"]: r["target"] for r in resp.json()["routes"]}
    assert "live-1" in routes
    assert routes["live-1"] != "http://stale:9999"  # live node_target, not the stored fallback


@pytest.mark.db
async def test_routes_falls_back_to_stored_target_when_node_target_gone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """#6: a running session whose device lost its node target (recovery backoff
    detached the AppiumNode) still appears in /routes with the stored router_target."""
    from app.appium_nodes.models import AppiumNode

    await seed_test_packs(db_session)
    _, device, node = await seed_host_and_running_node(db_session, identity=f"grid-guard-stale-{uuid.uuid4().hex[:8]}")
    db_session.add(
        Session(
            session_id="stored-1",
            device_id=device.id,
            status=SessionStatus.running,
            router_target="http://stored.example:4730",
        )
    )
    await db_session.commit()
    # Recovery backoff detached the node -> live node_target() is now None.
    await db_session.delete(await db_session.get(AppiumNode, node.id))
    await db_session.commit()

    resp = await client.get("/internal/grid/routes")
    assert resp.status_code == 200
    routes = {r["session_id"]: r["target"] for r in resp.json()["routes"]}
    assert routes.get("stored-1") == "http://stored.example:4730"


@pytest.mark.db
async def test_resume_claimed_falls_back_to_stored_target(
    db_session: AsyncSession,
) -> None:
    """#6: resume_claimed prefers a recomputed live target, else the stored one."""
    from app.appium_nodes.models import AppiumNode

    await seed_test_packs(db_session)
    _, device, node = await seed_host_and_running_node(db_session, identity=f"grid-guard-resume-{uuid.uuid4().hex[:8]}")
    row = Session(
        session_id="alloc-resume-1",
        device_id=device.id,
        status=SessionStatus.pending,
        router_target="http://stored.example:4730",
    )
    db_session.add(row)
    await db_session.flush()
    ticket = GridSessionQueueTicket(
        requested_body=_body(platformName="Android"),
        status=GridQueueStatus.claimed,
        session_row_id=row.id,
    )
    db_session.add(ticket)
    await db_session.flush()
    # Detach the node so node_target() is None and the stored fallback is used.
    await db_session.delete(await db_session.get(AppiumNode, node.id))
    await db_session.flush()

    result = await _service().resume_claimed(db_session, ticket=ticket)
    assert result is not None
    assert result.target == "http://stored.example:4730"
    assert ticket.status == GridQueueStatus.claimed
