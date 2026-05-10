"""Reconciler convergence integration tests with real DB rows and mocked agent calls."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from app.models.appium_node import AppiumNode, NodeState
from app.services.node_service_types import TemporaryNodeHandle
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]


class _SharedSessionContext:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def __aenter__(self) -> AsyncSession:
        return self._db

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def _session_factory(db: AsyncSession) -> object:
    def _factory() -> _SharedSessionContext:
        return _SharedSessionContext(db)

    return _factory


async def test_reconciler_starts_agent_when_desired_running_and_no_observed(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="conv-start", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=0,
        grid_url="http://hub:4444",
        state=NodeState.stopped,
        desired_state=NodeState.running,
        desired_port=4723,
    )
    db_session.add(node)
    await db_session.commit()

    from app.services import appium_reconciler

    start_mock = AsyncMock(
        return_value=TemporaryNodeHandle(
            port=4723,
            pid=12345,
            active_connection_target=device.identity_value,
            agent_base="http://agent",
            owner_key=f"device:{device.id}",
        )
    )

    with (
        patch.object(
            appium_reconciler, "agent_health", new=AsyncMock(return_value={"appium_processes": {"running_nodes": []}})
        ),
        patch.object(appium_reconciler, "async_session", new=_session_factory(db_session)),
        patch.object(appium_reconciler, "start_temporary_node", new=start_mock),
        patch.object(appium_reconciler, "stop_temporary_node", new=AsyncMock()),
    ):
        await appium_reconciler.run_one_cycle_for_test()

    await db_session.refresh(node)
    assert node.state == NodeState.running
    assert node.port == 4723
    assert node.pid == 12345
    start_mock.assert_awaited_once()


async def test_reconciler_stops_agent_when_desired_stopped_and_observed(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="conv-stop", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        pid=12345,
        state=NodeState.running,
        desired_state=NodeState.stopped,
        desired_port=None,
        active_connection_target=device.identity_value,
    )
    db_session.add(node)
    await db_session.commit()

    from app.services import appium_reconciler

    stop_mock = AsyncMock(return_value=True)
    payload = {
        "appium_processes": {
            "running_nodes": [
                {
                    "port": 4723,
                    "pid": 12345,
                    "connection_target": device.identity_value,
                    "platform_id": device.platform_id,
                }
            ],
        }
    }
    with (
        patch.object(appium_reconciler, "agent_health", new=AsyncMock(return_value=payload)),
        patch.object(appium_reconciler, "async_session", new=_session_factory(db_session)),
        patch.object(appium_reconciler, "start_temporary_node", new=AsyncMock()),
        patch.object(appium_reconciler, "stop_temporary_node", new=stop_mock),
    ):
        await appium_reconciler.run_one_cycle_for_test()

    await db_session.refresh(node)
    assert node.state == NodeState.stopped
    assert node.pid is None
    stop_mock.assert_awaited_once()
