"""Internal grid create-session contract tests."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from app.devices.services.health import DeviceHealthService
from app.grid import router_internal, session_create
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.runs.models import RunState, TestRun
from app.sessions.models import Session, SessionStatus
from tests.helpers import seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


@pytest.mark.db
async def test_mark_target_node_down_marks_error_and_advances_revision(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _, device, node = await seed_host_and_running_node(
        db_session,
        identity=f"grid-node-down-{uuid.uuid4().hex[:8]}",
    )
    previous_revision = node.health_observation_revision

    await session_create.mark_target_node_down(
        db_session_maker,
        DeviceHealthService(publisher=event_bus),
        device_id=device.id,
    )

    await db_session.refresh(node)
    assert node.health_running is False
    assert node.health_state == "error"
    assert node.health_observation_revision > previous_revision


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
        max_create_timeout_sec: float | None = None,
    ) -> session_create.CreateOutcome:
        _ = max_create_timeout_sec
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
@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_message"),
    [
        (
            session_create.CreateOutcome(
                kind="w3c_rejected",
                appium_status=500,
                appium_body={"value": {"error": "session not created"}},
            ),
            "create_failed",
            None,
        ),
        (
            session_create.CreateOutcome(kind="promotion_failed", message="allocation no longer pending"),
            "create_error",
            "allocation no longer pending",
        ),
    ],
    ids=["w3c-rejected", "promotion-failed"],
)
async def test_create_session_preserves_router_wire_status_contract(
    client: AsyncClient,
    seeded_available_device: Device,
    monkeypatch: pytest.MonkeyPatch,
    outcome: session_create.CreateOutcome,
    expected_status: str,
    expected_message: str | None,
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
        return outcome

    monkeypatch.setattr(router_internal.session_create, "create_and_promote", fake_create)
    resp = await client.post("/internal/grid/create-session", json={"body": _body(), "ticket": None, "run_id": None})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == expected_status
    assert data["appium_status"] == (500 if outcome.kind == "w3c_rejected" else None)
    assert data["appium_body"] == (
        {"value": {"error": "session not created"}} if outcome.kind == "w3c_rejected" else None
    )
    assert data["message"] == expected_message


@pytest.mark.db
async def test_retry_excludes_dead_target_then_creates(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, first_device, _ = await seed_host_and_running_node(
        db_session,
        identity=f"grid-retry-a-{uuid.uuid4().hex[:8]}",
        port=4730,
    )
    _, second_device, _ = await seed_host_and_running_node(
        db_session,
        identity=f"grid-retry-b-{uuid.uuid4().hex[:8]}",
        port=4731,
    )
    device_ids = {first_device.id, second_device.id}
    attempted_device_ids: list[uuid.UUID] = []
    marked_down_device_ids: list[uuid.UUID] = []

    original_mark_target_node_down = router_internal.session_create.mark_target_node_down

    async def recording_mark_target_node_down(
        db_factory: session_create.DbFactory,
        health: DeviceHealthService,
        *,
        device_id: uuid.UUID,
    ) -> None:
        marked_down_device_ids.append(device_id)
        await original_mark_target_node_down(db_factory, health, device_id=device_id)

    async def fake_create(
        db_factory: session_create.DbFactory,
        allocation_service: AllocationService,
        *,
        allocation: AllocationResult,
        raw_body: bytes,
        claim_window_sec: int,
        max_create_timeout_sec: float | None = None,
    ) -> session_create.CreateOutcome:
        _ = raw_body, claim_window_sec, max_create_timeout_sec
        attempted_device_ids.append(allocation.device_id)
        if len(attempted_device_ids) == 1:
            async with db_factory() as db:
                await allocation_service.fail(
                    db,
                    allocation_id=allocation.allocation_id,
                    message="simulated unreachable target",
                )
                await db.commit()
            return session_create.CreateOutcome(
                kind="target_unreachable",
                message="upstream unreachable",
                allocation=allocation,
            )
        return _created_outcome(allocation=allocation, session_id="retry-created-session")

    monkeypatch.setattr(router_internal.session_create, "mark_target_node_down", recording_mark_target_node_down)
    monkeypatch.setattr(router_internal.session_create, "create_and_promote", fake_create)

    resp = await client.post(
        "/internal/grid/create-session",
        json={"body": _body(platformName="Android"), "ticket": None, "run_id": None},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "created"
    assert resp.json()["session_id"] == "retry-created-session"
    assert set(attempted_device_ids) == device_ids
    assert len(attempted_device_ids) == 2
    assert marked_down_device_ids == [attempted_device_ids[0]]


@pytest.mark.db
async def test_retry_stops_after_three_failed_targets(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device_ids: set[uuid.UUID] = set()
    for index in range(4):
        _, device, _ = await seed_host_and_running_node(
            db_session,
            identity=f"grid-retry-cap-{index}-{uuid.uuid4().hex[:8]}",
            port=4740 + index,
        )
        device_ids.add(device.id)
    await db_session.commit()
    attempted_device_ids: list[uuid.UUID] = []

    async def fake_create(
        db_factory: session_create.DbFactory,
        allocation_service: AllocationService,
        *,
        allocation: AllocationResult,
        raw_body: bytes,
        claim_window_sec: int,
        max_create_timeout_sec: float | None = None,
    ) -> session_create.CreateOutcome:
        _ = raw_body, claim_window_sec, max_create_timeout_sec
        attempted_device_ids.append(allocation.device_id)
        async with db_factory() as db:
            await allocation_service.fail(
                db,
                allocation_id=allocation.allocation_id,
                message="simulated unreachable target",
            )
            await db.commit()
        return session_create.CreateOutcome(
            kind="target_unreachable",
            message="upstream unreachable",
            allocation=allocation,
        )

    monkeypatch.setattr(router_internal.session_create, "create_and_promote", fake_create)

    response = await client.post(
        "/internal/grid/create-session",
        json={"body": _body(platformName="Android"), "ticket": None, "run_id": None},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "create_error"
    assert len(attempted_device_ids) == router_internal.MAX_TARGET_ATTEMPTS == 3
    assert set(attempted_device_ids) < device_ids


@pytest.mark.db
async def test_budget_exhaustion_fails_before_unfinishable_attempt(
    client: AsyncClient,
    seeded_available_device: Device,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = seeded_available_device
    create_budgets: list[float | None] = []

    async def fake_create(
        db_factory: session_create.DbFactory,
        allocation_service: AllocationService,
        *,
        allocation: AllocationResult,
        raw_body: bytes,
        claim_window_sec: int,
        max_create_timeout_sec: float | None = None,
    ) -> session_create.CreateOutcome:
        _ = raw_body, claim_window_sec
        create_budgets.append(max_create_timeout_sec)
        async with db_factory() as db:
            await allocation_service.fail(
                db,
                allocation_id=allocation.allocation_id,
                message="simulated unreachable target",
            )
            await db.commit()
        return session_create.CreateOutcome(
            kind="target_unreachable",
            message="upstream unreachable",
            allocation=allocation,
        )

    monkeypatch.setattr(router_internal.session_create, "create_and_promote", fake_create)

    resp = await client.post(
        "/internal/grid/create-session",
        headers={"X-Gridfleet-Create-Budget-Ms": "1000"},
        json={"body": _body(platformName="Android"), "ticket": None, "run_id": None},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "create_error"
    assert len(create_budgets) == 1
    assert create_budgets == [0.0]


@pytest.mark.db
async def test_delayed_replacement_does_not_start_below_retry_budget(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, first_device, _ = await seed_host_and_running_node(
        db_session,
        identity=f"grid-delayed-retry-a-{uuid.uuid4().hex[:8]}",
        port=4732,
    )
    await db_session.commit()
    attempted_device_ids: list[uuid.UUID] = []
    replacement_device_id: uuid.UUID | None = None
    clock = SimpleNamespace(now=100.0)

    def monotonic() -> float:
        return clock.now

    async def fake_sleep(delay: float) -> None:
        nonlocal replacement_device_id
        _ = delay
        _, replacement, _ = await seed_host_and_running_node(
            db_session,
            identity=f"grid-delayed-retry-b-{uuid.uuid4().hex[:8]}",
            port=4733,
        )
        await db_session.commit()
        replacement_device_id = replacement.id
        clock.now += 0.1

    async def fake_create(
        db_factory: session_create.DbFactory,
        allocation_service: AllocationService,
        *,
        allocation: AllocationResult,
        raw_body: bytes,
        claim_window_sec: int,
        max_create_timeout_sec: float | None = None,
    ) -> session_create.CreateOutcome:
        _ = raw_body, claim_window_sec, max_create_timeout_sec
        attempted_device_ids.append(allocation.device_id)
        if len(attempted_device_ids) == 1:
            async with db_factory() as db:
                await allocation_service.fail(
                    db,
                    allocation_id=allocation.allocation_id,
                    message="simulated unreachable target",
                )
                await db.commit()
            return session_create.CreateOutcome(
                kind="target_unreachable",
                message="upstream unreachable",
                allocation=allocation,
            )
        return _created_outcome(allocation=allocation, session_id="late-replacement-session")

    monkeypatch.setattr(router_internal, "time", SimpleNamespace(monotonic=monotonic))
    monkeypatch.setattr(router_internal, "asyncio", SimpleNamespace(sleep=fake_sleep))
    monkeypatch.setattr(router_internal.session_create, "create_and_promote", fake_create)

    resp = await client.post(
        "/internal/grid/create-session",
        headers={"X-Gridfleet-Create-Budget-Ms": "20050"},
        json={"body": _body(platformName="Android"), "ticket": None, "run_id": None},
    )

    assert replacement_device_id is not None
    assert resp.status_code == 200
    assert resp.json()["status"] == "create_error"
    assert attempted_device_ids == [first_device.id]


@pytest.mark.db
async def test_w3c_rejected_does_not_retry(
    client: AsyncClient,
    seeded_available_device: Device,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = seeded_available_device
    create_call_count = 0

    async def fake_create(
        db_factory: session_create.DbFactory,
        allocation_service: AllocationService,
        *,
        allocation: AllocationResult,
        raw_body: bytes,
        claim_window_sec: int,
        max_create_timeout_sec: float | None = None,
    ) -> session_create.CreateOutcome:
        nonlocal create_call_count
        _ = raw_body, claim_window_sec, max_create_timeout_sec
        create_call_count += 1
        async with db_factory() as db:
            await allocation_service.fail(
                db,
                allocation_id=allocation.allocation_id,
                message="simulated W3C rejection",
            )
            await db.commit()
        return session_create.CreateOutcome(
            kind="w3c_rejected",
            appium_status=500,
            appium_body={"value": {"error": "session not created", "message": "capability rejected"}},
            allocation=allocation,
        )

    monkeypatch.setattr(router_internal.session_create, "create_and_promote", fake_create)

    resp = await client.post(
        "/internal/grid/create-session",
        json={"body": _body(platformName="Android"), "ticket": None, "run_id": None},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "create_failed"
    assert resp.json()["appium_status"] == 500
    assert resp.json()["appium_body"] == {"value": {"error": "session not created", "message": "capability rejected"}}
    assert create_call_count == 1


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
async def test_create_session_no_match_respects_short_router_budget(
    client: AsyncClient,
    seeded_available_device: Device,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = seeded_available_device
    clock = SimpleNamespace(now=100.0)
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock.now

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        clock.now += delay

    monkeypatch.setattr(router_internal, "time", SimpleNamespace(monotonic=monotonic))
    monkeypatch.setattr(router_internal, "asyncio", SimpleNamespace(sleep=fake_sleep))

    response = await client.post(
        "/internal/grid/create-session",
        headers={"X-Gridfleet-Create-Budget-Ms": "100"},
        json={"body": _body(platformName="iOS"), "ticket": None, "run_id": None},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert sleeps
    assert clock.now == pytest.approx(100.1)


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
        max_create_timeout_sec: float | None = None,
    ) -> session_create.CreateOutcome:
        _ = max_create_timeout_sec
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
        max_create_timeout_sec: float | None = None,
    ) -> session_create.CreateOutcome:
        _ = max_create_timeout_sec
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
