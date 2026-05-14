"""Phase 2: narrowed exception handling in nodes router (Sites 1 & 2)."""

import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import reconciler_agent as node_manager
from app.main import app
from tests.helpers import create_device_record, create_host
from tests.pack.factories import seed_test_packs

HOST_PAYLOAD = {
    "hostname": "nodes-exc-host",
    "ip": "10.0.0.41",
    "os_type": "linux",
    "agent_port": 5100,
}

DEVICE_PAYLOAD: dict[str, Any] = {
    "pack_id": "appium-uiautomator2",
    "platform_id": "android_mobile",
    "identity_scheme": "android_serial",
    "identity_scope": "host",
    "os_version": "14",
    "operational_state": "available",
    "verified": True,
}


@pytest_asyncio.fixture(autouse=True)
async def seed_packs(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()


@pytest_asyncio.fixture
async def host_id(client: AsyncClient) -> str:
    host = await create_host(client, **HOST_PAYLOAD)
    return str(host["id"])


async def _make_device(db_session: AsyncSession, hid: str) -> str:
    uid = uuid.uuid4().hex[:8]
    device = await create_device_record(
        db_session,
        host_id=hid,
        identity_value=f"serial-{uid}",
        connection_target=f"serial-{uid}",
        name=f"Test Device {uid}",
        **DEVICE_PAYLOAD,
    )
    await db_session.commit()
    return str(device.id)


# ---------------------------------------------------------------------------
# Site 1: start_node — NodeManagerError → 400
# ---------------------------------------------------------------------------


async def test_start_node_node_manager_error_returns_400(
    client: AsyncClient,
    db_session: AsyncSession,
    host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NodeManagerError from start_node must map to HTTP 400."""
    device_id = await _make_device(db_session, host_id)

    async def _raise(*args: object, **kwargs: object) -> None:
        raise node_manager.NodeManagerError("simulated start failure")

    monkeypatch.setattr(node_manager, "start_node", _raise)

    resp = await client.post(f"/api/devices/{device_id}/node/start")
    assert resp.status_code == 400
    assert "simulated start failure" in resp.json()["error"]["message"]


async def test_start_node_port_conflict_error_returns_400(
    client: AsyncClient,
    db_session: AsyncSession,
    host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NodePortConflictError (subclass of NodeManagerError) must also map to HTTP 400."""
    device_id = await _make_device(db_session, host_id)

    async def _raise(*args: object, **kwargs: object) -> None:
        raise node_manager.NodePortConflictError("port already in use")

    monkeypatch.setattr(node_manager, "start_node", _raise)

    resp = await client.post(f"/api/devices/{device_id}/node/start")
    assert resp.status_code == 400
    assert "port already in use" in resp.json()["error"]["message"]


async def test_start_node_unexpected_exception_bubbles_to_500(
    db_session: AsyncSession,
    host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected exceptions must NOT be swallowed by the narrowed except — they bubble to 500."""
    from app.core.database import get_db

    device_id = await _make_device(db_session, host_id)

    async def _raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(node_manager, "start_node", _raise)

    from collections.abc import AsyncGenerator

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        transport = ASGITransport(app=app, raise_app_exceptions=False)  # type: ignore[call-arg]
        async with AsyncClient(transport=transport, base_url="http://test") as uncaught_client:
            resp = await uncaught_client.post(f"/api/devices/{device_id}/node/start")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Site 2: stop_node — NodeManagerError → 400
# ---------------------------------------------------------------------------


async def test_stop_node_node_manager_error_returns_400(
    client: AsyncClient,
    db_session: AsyncSession,
    host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NodeManagerError from stop_node must map to HTTP 400."""
    device_id = await _make_device(db_session, host_id)
    # Add a running node so the stop guard passes
    db_session.add(
        AppiumNode(
            device_id=uuid.UUID(device_id),
            port=4723,
            grid_url="http://hub:4444",
            pid=12345,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            active_connection_target=f"serial-{device_id[:8]}",
        )
    )
    await db_session.commit()

    async def _raise(*args: object, **kwargs: object) -> None:
        raise node_manager.NodeManagerError("simulated stop failure")

    monkeypatch.setattr(node_manager, "stop_node", _raise)

    resp = await client.post(f"/api/devices/{device_id}/node/stop")
    assert resp.status_code == 400
    assert "simulated stop failure" in resp.json()["error"]["message"]
