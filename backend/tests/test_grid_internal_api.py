"""Internal allocation endpoints — the contract surface the grid router (Plan B) consumes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
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
from app.runs.models import RunState, TestRun
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
async def test_allocate_invalid_body_400_carries_merge_detail(
    client: AsyncClient, seeded_available_device: Device
) -> None:
    """Wave-5 #26: the descriptive CapabilityMergeError text (e.g. "'firstMatch'
    must be a list of objects") must reach the W3C client in the 400 body instead
    of a hard-coded generic 'invalid capabilities'."""
    resp = await client.post("/internal/grid/allocate", json={"body": {"capabilities": {"firstMatch": "nope"}}})
    assert resp.status_code == 400
    data = resp.json()
    assert data["status"] == "invalid"
    assert "firstMatch" in data["message"], f"detail lost: {data['message']!r}"


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
async def test_confirm_retry_with_same_appium_id_is_idempotent_success(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    """F2: a confirm whose response was lost is retried with the SAME appium_session_id;
    the row is already running, so the retry returns 204 (no rollback)."""
    resp = await client.post("/internal/grid/allocate", json={"body": _body(platformName="Android")})
    allocation_id = resp.json()["allocation_id"]

    first = await client.post(
        f"/internal/grid/sessions/{allocation_id}/confirm", json={"appium_session_id": "appium-x"}
    )
    assert first.status_code == 204
    retry = await client.post(
        f"/internal/grid/sessions/{allocation_id}/confirm", json={"appium_session_id": "appium-x"}
    )
    assert retry.status_code == 204

    row = await db_session.get(Session, uuid.UUID(allocation_id))
    assert row is not None
    await db_session.refresh(row)
    assert row.status == SessionStatus.running
    assert row.session_id == "appium-x"


@pytest.mark.db
async def test_confirm_conflicting_appium_id_after_success_is_409(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    """F2: a second confirm carrying a DIFFERENT appium_session_id is a genuine conflict
    and must still 409 (the original running session is untouched)."""
    resp = await client.post("/internal/grid/allocate", json={"body": _body(platformName="Android")})
    allocation_id = resp.json()["allocation_id"]

    assert (
        await client.post(f"/internal/grid/sessions/{allocation_id}/confirm", json={"appium_session_id": "appium-a"})
    ).status_code == 204
    conflict = await client.post(
        f"/internal/grid/sessions/{allocation_id}/confirm", json={"appium_session_id": "appium-b"}
    )
    assert conflict.status_code == 409

    row = await db_session.get(Session, uuid.UUID(allocation_id))
    assert row is not None
    await db_session.refresh(row)
    assert row.session_id == "appium-a"


@pytest.mark.db
async def test_confirm_after_reaper_failed_row_is_409(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    """F2: a confirm after the reaper already failed the pending row is a genuine
    conflict (the row is error, not running) and must 409, not be accepted."""
    resp = await client.post("/internal/grid/allocate", json={"body": _body(platformName="Android")})
    allocation_id = resp.json()["allocation_id"]

    # Simulate the reaper failing the still-pending row out from under the confirm.
    await client.post(f"/internal/grid/sessions/{allocation_id}/fail", json={"message": "claim window expired"})

    late = await client.post(
        f"/internal/grid/sessions/{allocation_id}/confirm", json={"appium_session_id": "appium-late"}
    )
    assert late.status_code == 409


@pytest.mark.db
async def test_confirm_409_on_terminal_row_stamps_doomed_appium_id(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    """Wave-5 #7: a confirm rejected because the row is already terminal must record
    the reported Appium id on that row (placeholder swap). The router's rollback
    DELETE is best-effort; without the id nothing tracks the orphan, and the orphan
    sweep spares unknown ids while the device holds a new pending row."""
    resp = await client.post("/internal/grid/allocate", json={"body": _body(platformName="Android")})
    allocation_id = resp.json()["allocation_id"]

    await client.post(f"/internal/grid/sessions/{allocation_id}/fail", json={"message": "claim window expired"})
    late = await client.post(
        f"/internal/grid/sessions/{allocation_id}/confirm", json={"appium_session_id": "appium-doomed"}
    )
    assert late.status_code == 409

    row = await db_session.get(Session, uuid.UUID(allocation_id))
    assert row is not None
    await db_session.refresh(row)
    assert row.session_id == "appium-doomed"  # real id recorded for the orphan sweep
    assert row.status == SessionStatus.error  # row stays terminal


@pytest.mark.db
async def test_confirm_409_does_not_stamp_when_live_row_owns_the_id(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    """The doomed-id stamp must never claim an Appium id a live row legitimately owns
    (legacy register_session conflict): that session is alive and tracked, not an
    orphan, and stamping would let the sweep treat a live id as doomed."""
    resp = await client.post("/internal/grid/allocate", json={"body": _body(platformName="Android")})
    allocation_id = resp.json()["allocation_id"]
    await client.post(f"/internal/grid/sessions/{allocation_id}/fail", json={"message": "claim window expired"})

    # A legitimately-running row (legacy register path) already owns the id.
    db_session.add(Session(session_id="appium-live", device_id=None, status=SessionStatus.running))
    await db_session.commit()

    late = await client.post(
        f"/internal/grid/sessions/{allocation_id}/confirm", json={"appium_session_id": "appium-live"}
    )
    assert late.status_code == 409

    row = await db_session.get(Session, uuid.UUID(allocation_id))
    assert row is not None
    await db_session.refresh(row)
    assert row.session_id.startswith("alloc-")  # placeholder untouched


@pytest.mark.db
async def test_confirm_leaves_last_activity_at_null(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    """Confirm does NOT stamp last_activity_at: a running row with NULL activity means
    the client never issued a command. The first-command grace reap owns that case; the
    server-stamped activity flush is the only writer of last_activity_at."""
    resp = await client.post("/internal/grid/allocate", json={"body": _body(platformName="Android")})
    allocation_id = resp.json()["allocation_id"]
    assert (
        await client.post(f"/internal/grid/sessions/{allocation_id}/confirm", json={"appium_session_id": "appium-c"})
    ).status_code == 204

    row = await db_session.get(Session, uuid.UUID(allocation_id))
    assert row is not None
    await db_session.refresh(row)
    assert row.last_activity_at is None


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
async def test_activity_accepts_bare_session_id_list(
    client: AsyncClient, db_session: AsyncSession, two_running_sessions: tuple[str, str]
) -> None:
    """Wave-5 #12: the router only needs to say WHICH sessions were active — the
    backend stamps server-side now() regardless of caller timestamps. The endpoint
    accepts a bare id list; the legacy id->timestamp map stays accepted for
    deploy-order compatibility with older routers."""
    sid_a, sid_b = two_running_sessions
    resp = await client.post("/internal/grid/activity", json={"sessions": [sid_a, sid_b, "unknown"]})
    assert resp.status_code == 204

    stmt = select(Session).where(Session.session_id.in_((sid_a, sid_b)))
    rows = (await db_session.execute(stmt)).scalars().all()
    for row in rows:
        await db_session.refresh(row)
        assert row.last_activity_at is not None


@pytest.mark.db
async def test_activity_stamps_server_now_ignoring_caller_timestamps(
    client: AsyncClient, db_session: AsyncSession, two_running_sessions: tuple[str, str]
) -> None:
    """F5: the activity write ignores caller-supplied datetimes (router clock skew must
    not extend/defeat idle reaping) and stamps a single server-side now() for every
    reported session, freshly >= the request time."""
    sid_a, sid_b = two_running_sessions
    request_time = datetime.now(UTC)
    resp = await client.post(
        "/internal/grid/activity",
        # Wildly skewed caller timestamps (one far past, one far future) must both be
        # ignored in favor of the server clock.
        json={"sessions": {sid_a: "2000-01-01T00:00:00Z", sid_b: "2099-12-31T23:59:59Z"}},
    )
    assert resp.status_code == 204

    stmt = select(Session).where(Session.session_id.in_((sid_a, sid_b)))
    by_sid = {row.session_id: row for row in (await db_session.execute(stmt)).scalars().all()}
    for row in by_sid.values():
        await db_session.refresh(row)
    for sid in (sid_a, sid_b):
        stamped = by_sid[sid].last_activity_at
        assert stamped is not None
        # Server now(), not the caller value: at or after the request, never the future
        # caller datetime.
        assert stamped >= request_time - timedelta(seconds=5)
        assert stamped < datetime(2099, 1, 1, tzinfo=UTC)


@pytest.mark.db
async def test_internal_routes_not_in_openapi(client: AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    assert not any(p.startswith("/internal/grid") for p in spec["paths"])


@pytest.mark.db
async def test_allocate_rejects_unknown_run(client: AsyncClient, seeded_available_device: Device) -> None:
    resp = await client.post(
        "/internal/grid/allocate",
        json={"body": _body(platformName="Android"), "run_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["status"] == "invalid"
    assert "only active runs" in body["message"]


@pytest.mark.db
async def test_allocate_rejects_legacy_run_id_cap(client: AsyncClient, seeded_available_device: Device) -> None:
    resp = await client.post(
        "/internal/grid/allocate",
        json={"body": _body(platformName="Android", **{"gridfleet:run_id": "free"})},
    )
    assert resp.status_code == 400
    assert "no longer supported" in resp.json()["message"]


@pytest.mark.db
async def test_allocate_persists_run_binding_on_queued_ticket(client: AsyncClient, db_session: AsyncSession) -> None:
    """No eligible device -> queued; the ticket row must carry the run binding."""
    run = TestRun(
        id=uuid.uuid4(),
        name="internal-api-run-binding",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.commit()
    resp = await client.post(
        "/internal/grid/allocate",
        json={"body": _body(platformName="Android"), "run_id": str(run.id)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    ticket = await db_session.get(GridSessionQueueTicket, uuid.UUID(data["ticket"]))
    assert ticket is not None and ticket.run_id == run.id


@pytest.mark.db
async def test_confirm_stores_actual_capabilities(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    resp = await client.post("/internal/grid/allocate", json={"body": _body(platformName="Android")})
    allocation_id = resp.json()["allocation_id"]

    resp = await client.post(
        f"/internal/grid/sessions/{allocation_id}/confirm",
        json={
            "appium_session_id": "appium-caps-1",
            "appium_capabilities": {"platformName": "Android", "appium:systemPort": 8200},
        },
    )
    assert resp.status_code == 204

    row = await db_session.get(Session, uuid.UUID(allocation_id))
    assert row is not None
    await db_session.refresh(row)
    assert row.actual_capabilities == {"platformName": "Android", "appium:systemPort": 8200}


@pytest.mark.db
async def test_confirm_without_capabilities_leaves_null(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    resp = await client.post("/internal/grid/allocate", json={"body": _body(platformName="Android")})
    allocation_id = resp.json()["allocation_id"]

    resp = await client.post(
        f"/internal/grid/sessions/{allocation_id}/confirm", json={"appium_session_id": "appium-nocaps-1"}
    )
    assert resp.status_code == 204

    row = await db_session.get(Session, uuid.UUID(allocation_id))
    assert row is not None
    await db_session.refresh(row)
    assert row.actual_capabilities is None
