"""Admin endpoint tests for clearing stuck Appium transition tokens."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.models.appium_node import AppiumNode, NodeState
from app.models.device_event import DeviceEvent, DeviceEventType
from tests.helpers import create_device

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_admin_clear_transition_clears_token_and_records_event(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="adm-clear", verified=True)
    token = uuid.uuid4()
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        state=NodeState.running,
        desired_state=NodeState.running,
        desired_port=4723,
        transition_token=token,
        transition_deadline=datetime.now(UTC) + timedelta(seconds=120),
    )
    db_session.add(node)
    await db_session.commit()

    response = await client.post(
        f"/api/admin/appium-nodes/{node.id}/clear-transition",
        json={"reason": "stuck on agent restart"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["transition_token"] is None
    assert body["transition_deadline"] is None

    await db_session.refresh(node)
    assert node.transition_token is None
    events = (await db_session.execute(select(DeviceEvent).where(DeviceEvent.device_id == device.id))).scalars().all()
    assert any(
        event.event_type == DeviceEventType.desired_state_changed
        and event.details is not None
        and event.details.get("caller") == "admin_clear_transition"
        and event.details.get("actor") == "anonymous-admin"
        for event in events
    )


async def test_admin_clear_transition_404_when_node_missing(client: AsyncClient) -> None:
    response = await client.post(f"/api/admin/appium-nodes/{uuid.uuid4()}/clear-transition", json={})
    assert response.status_code == 404


async def test_admin_clear_transition_requires_admin_when_auth_enabled(
    client: AsyncClient,
) -> None:
    from fastapi import HTTPException

    from app.main import app
    from app.services.auth_dependencies import require_admin

    async def reject_admin() -> str:
        raise HTTPException(status_code=418, detail="admin dependency used")

    app.dependency_overrides[require_admin] = reject_admin
    try:
        response = await client.post(f"/api/admin/appium-nodes/{uuid.uuid4()}/clear-transition", json={})
    finally:
        app.dependency_overrides.pop(require_admin, None)

    assert response.status_code == 418
