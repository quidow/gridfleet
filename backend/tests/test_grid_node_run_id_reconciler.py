from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.appium_node import AppiumNode, NodeState
from app.services.grid_node_run_id_reconciler import converge_grid_run_id_once
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host


async def _seed_node(db_session: AsyncSession, host: Host, *, name: str) -> AppiumNode:
    device = await create_device(db_session, host_id=host.id, name=name, operational_state="available")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://grid:4444",
        desired_state=NodeState.running,
        pid=1234,
        active_connection_target=device.connection_target,
    )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(node)
    return node


@pytest.mark.db
@pytest.mark.asyncio
async def test_no_op_when_observed_matches_desired(db_session: AsyncSession, db_host: Host) -> None:
    node = await _seed_node(db_session, db_host, name="grid-run-id-match")
    run_id = uuid.uuid4()
    node.desired_grid_run_id = run_id
    node.grid_run_id = run_id
    await db_session.commit()
    rpc_client = AsyncMock()

    dispatched = await converge_grid_run_id_once(db_session, rpc_client=rpc_client)

    assert dispatched == 0
    rpc_client.reregister_grid_node.assert_not_called()


@pytest.mark.db
@pytest.mark.asyncio
async def test_dispatches_when_desired_differs(db_session: AsyncSession, db_host: Host) -> None:
    nodes = [
        await _seed_node(db_session, db_host, name="grid-run-id-diff-1"),
        await _seed_node(db_session, db_host, name="grid-run-id-diff-2"),
    ]
    target_run_id = uuid.uuid4()
    for node in nodes:
        node.desired_grid_run_id = target_run_id
        node.grid_run_id = None
    await db_session.commit()
    rpc_client = AsyncMock()
    rpc_client.reregister_grid_node = AsyncMock(return_value=target_run_id)

    dispatched = await converge_grid_run_id_once(db_session, rpc_client=rpc_client)

    assert dispatched == 2
    assert rpc_client.reregister_grid_node.call_count == 2
    refreshed = await db_session.execute(select(AppiumNode).where(AppiumNode.id.in_([node.id for node in nodes])))
    assert {node.grid_run_id for node in refreshed.scalars()} == {target_run_id}


@pytest.mark.db
@pytest.mark.asyncio
async def test_dispatches_free_pool_target(db_session: AsyncSession, db_host: Host) -> None:
    node = await _seed_node(db_session, db_host, name="grid-run-id-free")
    old_run_id = uuid.uuid4()
    node.desired_grid_run_id = None
    node.grid_run_id = old_run_id
    await db_session.commit()
    rpc_client = AsyncMock()
    rpc_client.reregister_grid_node = AsyncMock(return_value=None)

    dispatched = await converge_grid_run_id_once(db_session, rpc_client=rpc_client)

    assert dispatched == 1
    rpc_client.reregister_grid_node.assert_awaited_once()
    await db_session.refresh(node)
    assert node.grid_run_id is None
