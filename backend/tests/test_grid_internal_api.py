"""Internal allocation endpoints — the contract surface the grid router (Plan B) consumes."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device

from app.grid import router_internal
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.sessions.models import Session, SessionStatus
from tests.helpers import seed_host_and_running_node
from tests.pack.factories import seed_test_packs


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


@pytest_asyncio.fixture
async def two_running_sessions(db_session: AsyncSession) -> tuple[str, str]:
    """Two confirmed running sessions on distinct devices, returned by session_id."""
    await seed_test_packs(db_session)
    sids: list[str] = []
    for i in range(2):
        _, device, _ = await seed_host_and_running_node(db_session, identity=f"grid-act-{uuid.uuid4().hex[:8]}")
        sid = f"act-bulk-{i}"
        db_session.add(Session(session_id=sid, device_id=device.id, status=SessionStatus.running))
        sids.append(sid)
    await db_session.commit()
    return sids[0], sids[1]


@pytest.mark.db
async def test_allocate_immediate_match(client: AsyncClient, seeded_available_device: Device) -> None:
    resp = await client.post("/internal/grid/allocate", json={"body": _body(platformName="Android")})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "allocated"
    assert data["allocation_id"]
    assert data["target"].startswith("http://")
    assert data["claim_window_sec"] == 120


@pytest.mark.db
async def test_allocate_no_match_queues_and_ticket_is_reusable(
    client: AsyncClient, seeded_available_device: Device
) -> None:
    resp = await client.post("/internal/grid/allocate", json={"body": _body(platformName="iOS")})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    ticket = data["ticket"]
    assert ticket
    # pass the ticket back -> still queued, same ticket
    resp2 = await client.post("/internal/grid/allocate", json={"body": _body(platformName="iOS"), "ticket": ticket})
    assert resp2.status_code == 200
    assert resp2.json() == {
        "status": "queued",
        "allocation_id": None,
        "target": None,
        "ticket": ticket,
        "claim_window_sec": None,
    }


@pytest.mark.db
async def test_allocate_invalid_body_is_400(client: AsyncClient, seeded_available_device: Device) -> None:
    resp = await client.post(
        "/internal/grid/allocate", json={"body": {"desiredCapabilities": {"platformName": "Android"}}}
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["status"] == "invalid"
    assert data["message"]


@pytest.mark.db
async def test_cancel_ticket(client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device) -> None:
    resp = await client.post("/internal/grid/allocate", json={"body": _body(platformName="iOS")})
    ticket_id = resp.json()["ticket"]
    resp2 = await client.delete(f"/internal/grid/allocate/{ticket_id}")
    assert resp2.status_code == 204
    ticket = await db_session.get(GridSessionQueueTicket, uuid.UUID(ticket_id))
    assert ticket is not None
    await db_session.refresh(ticket)
    assert ticket.status == GridQueueStatus.cancelled


@pytest.mark.db
async def test_confirm_fail_ended_and_routes(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    # allocate -> confirm -> appears in routes -> ended -> gone from routes
    resp = await client.post("/internal/grid/allocate", json={"body": _body(platformName="Android")})
    allocation_id = resp.json()["allocation_id"]
    target = resp.json()["target"]

    resp = await client.post(f"/internal/grid/sessions/{allocation_id}/confirm", json={"appium_session_id": "appium-1"})
    assert resp.status_code == 204

    resp = await client.get("/internal/grid/routes")
    assert resp.status_code == 200
    assert {"session_id": "appium-1", "target": target} in resp.json()["routes"]

    resp = await client.post("/internal/grid/sessions/ended", json={"session_id": "appium-1"})
    assert resp.status_code == 204
    resp = await client.get("/internal/grid/routes")
    assert resp.json()["routes"] == []


@pytest.mark.db
async def test_allocate_claimed_ticket_retry_is_idempotent(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    """A retry carrying the already-claimed ticket returns the SAME allocation and does
    not claim a second device or create a second Session row (#2)."""
    resp = await client.post("/internal/grid/allocate", json={"body": _body(platformName="Android")})
    allocation_id = resp.json()["allocation_id"]

    # Recover the claimed ticket id (the Allocated response does not carry it; a real
    # router would have it from a prior queued response).
    ticket = (
        (
            await db_session.execute(
                select(GridSessionQueueTicket).where(GridSessionQueueTicket.session_row_id == uuid.UUID(allocation_id))
            )
        )
        .scalars()
        .first()
    )
    assert ticket is not None and ticket.status == GridQueueStatus.claimed

    sessions_before = (await db_session.execute(select(func.count()).select_from(Session))).scalar_one()

    resp2 = await client.post(
        "/internal/grid/allocate",
        json={"body": _body(platformName="Android"), "ticket": str(ticket.id)},
    )
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "allocated"
    assert resp2.json()["allocation_id"] == allocation_id

    sessions_after = (await db_session.execute(select(func.count()).select_from(Session))).scalar_one()
    assert sessions_after == sessions_before


@pytest.mark.db
async def test_confirm_unknown_allocation_is_409(client: AsyncClient, seeded_available_device: Device) -> None:
    resp = await client.post(f"/internal/grid/sessions/{uuid.uuid4()}/confirm", json={"appium_session_id": "x"})
    assert resp.status_code == 409


@pytest.mark.db
async def test_fail_releases_allocation(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    resp = await client.post("/internal/grid/allocate", json={"body": _body(platformName="Android")})
    allocation_id = resp.json()["allocation_id"]
    resp = await client.post(f"/internal/grid/sessions/{allocation_id}/fail", json={"message": "appium refused"})
    assert resp.status_code == 204
    row = await db_session.get(Session, uuid.UUID(allocation_id))
    assert row is not None
    await db_session.refresh(row)
    assert row.status == SessionStatus.error


@pytest.mark.db
async def test_activity_updates_last_activity_at(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    resp = await client.post("/internal/grid/allocate", json={"body": _body(platformName="Android")})
    allocation_id = resp.json()["allocation_id"]
    await client.post(f"/internal/grid/sessions/{allocation_id}/confirm", json={"appium_session_id": "act-1"})

    resp = await client.post(
        "/internal/grid/activity",
        json={"sessions": {"act-1": "2026-06-05T12:00:00Z", "unknown": "2026-06-05T12:00:00Z"}},
    )
    assert resp.status_code == 204
    row = await db_session.get(Session, uuid.UUID(allocation_id))
    assert row is not None
    await db_session.refresh(row)
    assert row.last_activity_at is not None


@pytest.mark.db
async def test_activity_bulk_update_round_trips_distinct_timestamps(
    client: AsyncClient, db_session: AsyncSession, two_running_sessions: tuple[str, str]
) -> None:
    """Rider #21: the single executemany activity write must land each session's own
    timestamp (no cross-talk between rows)."""
    sid_a, sid_b = two_running_sessions
    resp = await client.post(
        "/internal/grid/activity",
        json={"sessions": {sid_a: "2026-06-05T12:00:00Z", sid_b: "2026-06-05T13:30:00Z"}},
    )
    assert resp.status_code == 204

    stmt = select(Session).where(Session.session_id.in_((sid_a, sid_b)))
    by_sid = {row.session_id: row for row in (await db_session.execute(stmt)).scalars().all()}
    for row in by_sid.values():
        await db_session.refresh(row)
    assert by_sid[sid_a].last_activity_at is not None
    assert by_sid[sid_b].last_activity_at is not None
    # Distinct timestamps proves per-row binding, not a single shared value.
    assert by_sid[sid_a].last_activity_at != by_sid[sid_b].last_activity_at
    assert by_sid[sid_b].last_activity_at > by_sid[sid_a].last_activity_at


@pytest.mark.db
async def test_internal_routes_not_in_openapi(client: AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    assert not any(p.startswith("/internal/grid") for p in spec["paths"])
