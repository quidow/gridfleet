import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.services import state_write_guard
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.sessions.models import Session, SessionStatus
from tests.helpers import create_device_record
from tests.pack.factories import seed_test_packs

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
    assert data["grid"]["ready"] is True
    assert data["grid"]["value"]["ready"] is True
    assert data["grid"]["value"]["nodes"] == []
    assert data["grid"]["value"]["sessionQueueRequests"] == []
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
    """Grid status reflects running nodes and maps running sessions into slots."""
    device = await _seed_device(db_session, default_host_id, operational_state="available")

    with state_write_guard.bypass():
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

    assert len(data["grid"]["value"]["nodes"]) == 1
    assert data["grid"]["value"]["nodes"][0]["slots"] == [{"session": "sess-abc"}]
    assert data["active_sessions"] == 1


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
    assert len(data["grid"]["value"]["sessionQueueRequests"]) == 1


async def test_grid_queue(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    ticket = GridSessionQueueTicket(
        requested_body={
            "capabilities": {
                "alwaysMatch": {"platformName": "android", "appium:platformVersion": "14"},
                "firstMatch": [{"gridfleet:run_id": "run-123"}],
            }
        },
        status=GridQueueStatus.waiting,
    )
    db_session.add(ticket)
    # A non-waiting ticket must be excluded.
    db_session.add(
        GridSessionQueueTicket(
            requested_body={"capabilities": {"alwaysMatch": {"platformName": "ios"}}},
            status=GridQueueStatus.claimed,
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
    assert req["capabilities"]["gridfleet:run_id"] == "run-123"
    assert req["requestTimestamp"] == ticket.created_at.isoformat()
