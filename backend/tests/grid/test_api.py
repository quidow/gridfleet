import uuid
from typing import TYPE_CHECKING

import pytest_asyncio

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.sessions.models import Session, SessionStatus
from tests.helpers import create_device_record
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

DEVICE_PAYLOAD = {
    "identity_value": "emulator-5554",
    "name": "Pixel 7 Emulator",
    "pack_id": "appium-uiautomator2",
    "platform_id": "android_mobile",
    "identity_scheme": "android_serial",
    "identity_scope": "host",
    "os_version": "14",
}


@pytest_asyncio.fixture(autouse=True)
async def seed_packs(db_session: AsyncSession) -> None:
    """Seed driver packs so the assert_runnable gate passes in all tests."""
    await seed_test_packs(db_session)
    await db_session.commit()


async def _seed_device(db_session: AsyncSession, default_host_id: str, **overrides: object) -> object:
    return await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value=DEVICE_PAYLOAD["identity_value"],
        connection_target=DEVICE_PAYLOAD["identity_value"],
        name=DEVICE_PAYLOAD["name"],
        pack_id=DEVICE_PAYLOAD["pack_id"],
        platform_id=DEVICE_PAYLOAD["platform_id"],
        identity_scheme=DEVICE_PAYLOAD["identity_scheme"],
        identity_scope=DEVICE_PAYLOAD["identity_scope"],
        os_version=DEVICE_PAYLOAD["os_version"],
        **overrides,
    )


async def test_grid_status(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _seed_device(db_session, default_host_id)

    resp = await client.get("/api/grid/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["ready"] is True
    assert data["active_session_ids"] == []
    assert data["queued_request_ids"] == []
    assert data["running_node_count"] == 0
    assert data["registry"]["device_count"] == 1
    assert data["registry"]["devices"][0]["identity_value"] == "emulator-5554"
    assert data["registry"]["devices"][0]["platform_id"] == "android_mobile"
    assert "platform" not in data["registry"]["devices"][0]
    assert data["registry"]["devices"][0]["node_state"] is None
    assert data["active_sessions"] == 0
    assert data["queue_size"] == 0


async def test_grid_status_with_running_node_and_session(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    """Grid status reflects running nodes and lists active session ids."""
    device = await _seed_device(db_session, default_host_id, operational_state="available")

    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            pid=9999,
            active_connection_target="emulator-5554",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )
    )
    db_session.add(
        Session(
            session_id="sess-abc",
            status=SessionStatus.running,
            device_id=device.id,
        )
    )
    await db_session.commit()

    resp = await client.get("/api/grid/status")

    assert resp.status_code == 200
    data = resp.json()
    dev_entry = data["registry"]["devices"][0]
    assert dev_entry["platform_id"] == "android_mobile"
    assert "platform" not in dev_entry
    assert dev_entry["node_state"] == "running"
    assert dev_entry["node_port"] == 4723
    assert dev_entry["operational_state"] == "available"

    assert data["running_node_count"] == 1
    assert data["active_session_ids"] == ["sess-abc"]
    assert data["active_sessions"] == 1


async def test_grid_status_counts_pending_allocation(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    """Wave-5 re-review B2: a pending grid allocation (allocate->confirm window)
    already claims its device — the public status must count it, not report the
    device as free."""
    device = await _seed_device(db_session, default_host_id, operational_state="busy")

    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            pid=9999,
            active_connection_target="emulator-5554",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )
    )
    db_session.add(
        Session(
            session_id="alloc-pending-placeholder",
            status=SessionStatus.pending,
            device_id=device.id,
        )
    )
    await db_session.commit()

    resp = await client.get("/api/grid/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["active_sessions"] == 1
    assert data["active_session_ids"] == ["alloc-pending-placeholder"]
    assert data["running_node_count"] == 1


async def test_grid_status_queue_size(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _seed_device(db_session, default_host_id)
    db_session.add(
        GridSessionQueueTicket(
            requested_body={"capabilities": {"alwaysMatch": {"platformName": "android"}}},
            status=GridQueueStatus.waiting,
        )
    )
    await db_session.commit()

    resp = await client.get("/api/grid/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["queue_size"] == 1
    assert len(data["queued_request_ids"]) == 1


async def test_grid_queue(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    run_id = uuid.uuid4()
    ticket = GridSessionQueueTicket(
        requested_body={
            "capabilities": {
                "alwaysMatch": {"platformName": "android", "appium:platformVersion": "14"},
                "firstMatch": [{"appium:automationName": "UiAutomator2"}],
            }
        },
        status=GridQueueStatus.waiting,
        run_id=run_id,
    )
    db_session.add(ticket)
    # A non-waiting ticket must be excluded.
    db_session.add(
        GridSessionQueueTicket(
            requested_body={"capabilities": {"alwaysMatch": {"platformName": "ios"}}},
            status=GridQueueStatus.expired,
        )
    )
    await db_session.commit()
    await db_session.refresh(ticket)

    resp = await client.get("/api/grid/queue")

    assert resp.status_code == 200
    data = resp.json()
    assert data["queue_size"] == 1
    assert len(data["requests"]) == 1
    req = data["requests"][0]
    assert req["requestId"] == str(ticket.id)
    assert req["capabilities"]["platformName"] == "android"
    assert req["capabilities"]["appium:platformVersion"] == "14"
    assert req["capabilities"]["appium:automationName"] == "UiAutomator2"
    assert req["requestTimestamp"] == ticket.created_at.isoformat()
    # Run attribution comes from the ticket row (run-scoped endpoint), not from
    # capabilities — the gridfleet:run_id cap is retired (review F1).
    assert req["runId"] == str(run_id)


async def test_grid_queue_free_ticket_has_null_run(client: AsyncClient, db_session: AsyncSession) -> None:
    db_session.add(
        GridSessionQueueTicket(
            requested_body={"capabilities": {"alwaysMatch": {"platformName": "android"}}},
            status=GridQueueStatus.waiting,
        )
    )
    await db_session.commit()

    resp = await client.get("/api/grid/queue")

    assert resp.status_code == 200
    assert resp.json()["requests"][0]["runId"] is None
