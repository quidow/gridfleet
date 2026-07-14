"""Internal grid create-session contract tests."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from app.grid import router_internal, session_create
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.runs.models import RunState, TestRun
from app.sessions.models import Session, SessionStatus
from tests.helpers import seed_host_and_running_node
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.grid.allocation import AllocationResult, AllocationService

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _body(**caps: str) -> dict[str, Any]:
    return {"capabilities": {"alwaysMatch": caps, "firstMatch": [{}]}}


@pytest.fixture(autouse=True)
def fast_long_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_internal, "LONG_POLL_SEC", 0.3)
    monkeypatch.setattr(router_internal, "RETRY_INTERVAL_SEC", 0.05)


@pytest_asyncio.fixture
async def seeded_available_device(db_session: AsyncSession) -> Device:
    await seed_test_packs(db_session)
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"grid-api-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    return device


def _created_outcome(
    *, allocation: AllocationResult, session_id: str = "created-session"
) -> session_create.CreateOutcome:
    return session_create.CreateOutcome(
        kind="created",
        appium_status=200,
        appium_body={"value": {"sessionId": session_id, "capabilities": {}}},
        session_id=session_id,
        allocation=allocation,
    )


@pytest.mark.db
async def test_create_session_claims_then_creates(
    client: AsyncClient, seeded_available_device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_create(
        db_factory: session_create.DbFactory,
        allocation_service: AllocationService,
        *,
        allocation: AllocationResult,
        raw_body: bytes,
        claim_window_sec: int,
    ) -> session_create.CreateOutcome:
        calls.append({"raw": raw_body, "window": claim_window_sec, "target": allocation.target})
        return _created_outcome(allocation=allocation)

    monkeypatch.setattr(router_internal.session_create, "create_and_promote", fake_create)
    resp = await client.post(
        "/internal/grid/create-session",
        json={"body": {"capabilities": {"alwaysMatch": {}}}, "ticket": None, "run_id": None},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "created"
    assert data["session_id"] == "created-session"
    assert data["target"] == calls[0]["target"]
    assert json.loads(calls[0]["raw"]) == {"capabilities": {"alwaysMatch": {}}}


@pytest.mark.db
async def test_create_session_relays_w3c_rejected(
    client: AsyncClient, seeded_available_device: Device, monkeypatch: pytest.MonkeyPatch
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
            kind="w3c_rejected", appium_status=500, appium_body={"value": {"error": "session not created"}}
        )

    monkeypatch.setattr(router_internal.session_create, "create_and_promote", fake_create)
    resp = await client.post("/internal/grid/create-session", json={"body": _body(), "ticket": None, "run_id": None})
    assert resp.status_code == 200
    assert resp.json()["status"] == "w3c_rejected"
    assert resp.json()["appium_status"] == 500


@pytest.mark.db
async def test_create_session_no_match_queues_and_reuses_ticket(
    client: AsyncClient, seeded_available_device: Device
) -> None:
    resp = await client.post("/internal/grid/create-session", json={"body": _body(platformName="iOS")})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    ticket = data["ticket"]
    resp2 = await client.post(
        "/internal/grid/create-session", json={"body": _body(platformName="iOS"), "ticket": ticket}
    )
    assert resp2.status_code == 200
    assert resp2.json() == {
        "status": "queued",
        "session_id": None,
        "target": None,
        "device_id": None,
        "appium_status": None,
        "appium_body": None,
        "ticket": ticket,
        "message": None,
    }


@pytest.mark.db
async def test_create_session_invalid_body_is_400(client: AsyncClient, seeded_available_device: Device) -> None:
    resp = await client.post(
        "/internal/grid/create-session", json={"body": {"capabilities": {"firstMatch": "invalid"}}}
    )
    assert resp.status_code == 400
    assert resp.json()["status"] == "invalid"
    assert "firstMatch" in resp.json()["message"]


@pytest.mark.db
async def test_cancel_ticket_moves_to_tickets_route(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    resp = await client.post("/internal/grid/create-session", json={"body": _body(platformName="iOS")})
    ticket_id = resp.json()["ticket"]
    resp2 = await client.delete(f"/internal/grid/tickets/{ticket_id}")
    assert resp2.status_code == 204
    ticket = await db_session.get(GridSessionQueueTicket, uuid.UUID(ticket_id))
    assert ticket is not None
    await db_session.refresh(ticket)
    assert ticket.status == GridQueueStatus.cancelled


@pytest.mark.db
async def test_create_session_run_binding_stays_on_queued_ticket(client: AsyncClient, db_session: AsyncSession) -> None:
    run = TestRun(
        id=uuid.uuid4(),
        name="create-session-run",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.commit()
    resp = await client.post(
        "/internal/grid/create-session", json={"body": _body(platformName="Android"), "run_id": str(run.id)}
    )
    assert resp.status_code == 200
    ticket = await db_session.get(GridSessionQueueTicket, uuid.UUID(resp.json()["ticket"]))
    assert ticket is not None and ticket.run_id == run.id


@pytest.mark.db
async def test_create_session_resume_fails_interrupted_pending(
    client: AsyncClient,
    db_session: AsyncSession,
    seeded_available_device: Device,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket_id = uuid.uuid4()
    row = Session(
        session_id=f"alloc-{uuid.uuid4()}",
        device_id=seeded_available_device.id,
        status=SessionStatus.pending,
        ticket_id=ticket_id,
        router_target="http://host:4730",
    )
    db_session.add(row)
    await db_session.commit()

    async def fake_create(
        db_factory: session_create.DbFactory,
        allocation_service: AllocationService,
        *,
        allocation: AllocationResult,
        raw_body: bytes,
        claim_window_sec: int,
    ) -> session_create.CreateOutcome:
        return _created_outcome(allocation=allocation)

    monkeypatch.setattr(router_internal.session_create, "create_and_promote", fake_create)
    resp = await client.post(
        "/internal/grid/create-session", json={"body": _body(platformName="Android"), "ticket": str(ticket_id)}
    )
    assert resp.status_code == 200 and resp.json()["status"] == "created"
    await db_session.refresh(row)
    assert row.status == SessionStatus.error


@pytest.mark.db
async def test_routes_activity_and_ended_remain_available(
    client: AsyncClient,
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
                db, allocation_id=allocation.allocation_id, appium_session_id="route-session"
            )
            await db.commit()
        return _created_outcome(allocation=allocation, session_id="route-session")

    monkeypatch.setattr(router_internal.session_create, "create_and_promote", fake_create)
    created = await client.post("/internal/grid/create-session", json={"body": _body(platformName="Android")})
    assert created.status_code == 200
    target = created.json()["target"]
    routes = await client.get("/internal/grid/routes")
    assert {"session_id": "route-session", "target": target} in routes.json()["routes"]
    assert (await client.post("/internal/grid/activity", json={"sessions": ["route-session"]})).status_code == 204
    assert (await client.post("/internal/grid/sessions/ended", json={"session_id": "route-session"})).status_code == 204
    assert (await client.get("/internal/grid/routes")).json()["routes"] == []
