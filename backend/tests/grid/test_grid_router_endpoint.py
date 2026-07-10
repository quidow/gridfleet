from typing import TYPE_CHECKING

import pytest

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.sessions.models import Session, SessionStatus
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]

DEVICE_PAYLOAD = {
    "identity_value": "device-router-1",
    "name": "Pixel 7",
    "pack_id": "appium-uiautomator2",
    "platform_id": "android_mobile",
    "identity_scheme": "serial",
    "identity_scope": "host",
    "os_version": "14",
}


async def _seed_device(db: AsyncSession, host_id: str, identity: str, name: str, **overrides: object) -> object:
    # overrides merged LAST so a test can replace pack_id/platform_id/operational_state
    # without colliding with the DEVICE_PAYLOAD defaults (no duplicate-kwarg TypeError).
    payload = {**DEVICE_PAYLOAD, "identity_value": identity, "connection_target": identity, "name": name, **overrides}
    return await create_device_record(db, host_id=host_id, **payload)


async def _add_running_node(
    db: AsyncSession, device_id: object, port: int, *, accepting_new_sessions: bool = True
) -> None:
    db.add(
        AppiumNode(
            device_id=device_id,
            port=port,
            pid=9999,
            active_connection_target="emulator-5554",
            desired_state=AppiumDesiredState.running,
            desired_port=port,
            accepting_new_sessions=accepting_new_sessions,
        )
    )


async def test_grid_router_shape_and_counts(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    avail = await _seed_device(db_session, default_host_id, "dev-a", "Pixel 7", operational_state="available")
    busy = await _seed_device(db_session, default_host_id, "dev-b", "iPhone 15", operational_state="busy")
    await _add_running_node(db_session, avail.id, 4723)
    await _add_running_node(db_session, busy.id, 4724)
    db_session.add(Session(session_id="sess-busy", status=SessionStatus.running, device_id=busy.id))
    db_session.add(
        GridSessionQueueTicket(
            requested_body={"capabilities": {"alwaysMatch": {"platformName": "android"}}},
            status=GridQueueStatus.waiting,
        )
    )
    await db_session.commit()

    resp = await client.get("/api/grid/router")
    assert resp.status_code == 200
    body = resp.json()

    counts = body["counts"]
    assert counts["registered"] == 2
    assert counts["available"] == 1
    assert counts["busy"] == 1
    assert counts["running"] == 2
    assert counts["active_sessions"] == 1
    assert counts["queue_depth"] == 1
    # `eligible` is the Router "open" count: available ∧ node-viable ∧ accepting ∧ no
    # live session — the same gate the allocator applies. Only `avail` qualifies (busy
    # is not available).
    assert counts["eligible"] == 1

    nodes = {n["device_name"]: n for n in body["nodes"]}
    assert set(nodes) == {"Pixel 7", "iPhone 15"}
    assert nodes["Pixel 7"]["node_effective_state"] == "running"
    assert "gridfleet:deviceId" in nodes["Pixel 7"]["stereotype"]
    assert nodes["Pixel 7"]["session_id"] is None
    assert nodes["iPhone 15"]["session_id"] == "sess-busy"
    assert nodes["iPhone 15"]["session_target"].startswith("http://")

    assert len(body["queue"]) == 1


async def test_grid_router_eligible_excludes_warm_parked_device(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    """`eligible` follows the allocator's gate, not the coarse operational axis.

    Two `available` devices: one with a warm, accepting node (open) and one warm-parked
    by cooldown (`accepting_new_sessions=False`). Both read `available`, but only the
    accepting one is `eligible`; the parked node reports `unavailable_reason="cooldown"`
    while the open one reports `None`.
    """
    open_dev = await _seed_device(db_session, default_host_id, "dev-open", "Open", operational_state="available")
    parked = await _seed_device(db_session, default_host_id, "dev-parked", "Parked", operational_state="available")
    await _add_running_node(db_session, open_dev.id, 4731)
    await _add_running_node(db_session, parked.id, 4732, accepting_new_sessions=False)
    await db_session.commit()

    body = (await client.get("/api/grid/router")).json()

    counts = body["counts"]
    assert counts["available"] == 2
    assert counts["eligible"] == 1

    nodes = {n["device_name"]: n for n in body["nodes"]}
    assert nodes["Open"]["unavailable_reason"] is None
    assert nodes["Parked"]["unavailable_reason"] == "cooldown"


async def test_grid_router_degrades_when_pack_unresolved(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await _seed_device(db_session, default_host_id, "dev-c", "Mystery", pack_id="does-not-exist", platform_id="nope")
    await db_session.commit()

    resp = await client.get("/api/grid/router")
    assert resp.status_code == 200
    node = next(n for n in resp.json()["nodes"] if n["device_name"] == "Mystery")
    assert "gridfleet:deviceId" in node["stereotype"]  # sparse but never empty


async def test_grid_router_many_same_pack_devices(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    for i in range(3):
        await _seed_device(db_session, default_host_id, f"dev-same-{i}", f"Dev {i}")
    await db_session.commit()

    resp = await client.get("/api/grid/router")
    assert resp.status_code == 200
    assert all("gridfleet:deviceId" in n["stereotype"] for n in resp.json()["nodes"])
