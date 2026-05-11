"""Converge Selenium Grid node run-routing stereotypes."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import select

from app import metrics_recorders
from app.database import async_session
from app.models.appium_node import AppiumNode
from app.models.device import Device
from app.models.host import Host, HostStatus
from app.observability import get_logger, observe_background_loop, schedule_background_loop
from app.services import device_locking
from app.services.agent_operations import grid_node_reregister
from app.services.control_plane_leader import LeadershipLost, assert_current_leader
from app.services.settings_service import settings_service

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)
LOOP_NAME = "grid_node_run_id_reconciler"


@dataclass(frozen=True)
class GridNodeReregisterRequest:
    device_id: uuid.UUID
    host_id: uuid.UUID
    host: str
    agent_port: int
    node_id: uuid.UUID
    target_run_id: uuid.UUID | None


class GridNodeReregisterRpc(Protocol):
    async def reregister_grid_node(
        self,
        *,
        host: str,
        agent_port: int,
        node_id: uuid.UUID,
        target_run_id: uuid.UUID | None,
    ) -> uuid.UUID | None:
        """Tell the agent to re-register and return the observed grid_run_id."""


class AgentOperationsGridNodeReregisterRpc:
    def __init__(self, *, timeout: float | int = 20) -> None:
        self._timeout = timeout

    async def reregister_grid_node(
        self,
        *,
        host: str,
        agent_port: int,
        node_id: uuid.UUID,
        target_run_id: uuid.UUID | None,
    ) -> uuid.UUID | None:
        return await grid_node_reregister(
            host,
            agent_port,
            node_id,
            target_run_id=target_run_id,
            timeout=self._timeout,
        )


def default_grid_node_reregister_rpc(*, timeout: float | int = 20) -> AgentOperationsGridNodeReregisterRpc:
    return AgentOperationsGridNodeReregisterRpc(timeout=timeout)


async def grid_node_run_id_reconciler_loop() -> None:
    interval = float(settings_service.get("appium_reconciler.interval_sec"))
    await schedule_background_loop(LOOP_NAME, interval)
    while True:
        cycle_start = time.monotonic()
        try:
            async with observe_background_loop(LOOP_NAME, interval).cycle(), async_session() as db:
                await assert_current_leader(db)
                await converge_grid_run_id_once(db, rpc_client=default_grid_node_reregister_rpc())
        except LeadershipLost as exc:
            logger.error(
                "grid_node_run_id_reconciler_leadership_lost",
                reason=str(exc),
                action="exiting_process_to_prevent_split_brain",
            )
            os._exit(70)
        except Exception:
            logger.exception("grid_node_run_id_reconciler_cycle_failed")
        logger.debug("grid_node_run_id_reconciler_cycle_finished duration_sec=%s", time.monotonic() - cycle_start)
        interval = float(settings_service.get("appium_reconciler.interval_sec"))
        await asyncio.sleep(interval)


async def converge_grid_run_id_once(
    db: AsyncSession,
    *,
    rpc_client: GridNodeReregisterRpc,
) -> int:
    """Single reconciler tick. Returns the count of successful re-registrations."""
    requests = await _load_diverging_requests(db)
    dispatched = 0

    for request in requests:
        try:
            observed = await rpc_client.reregister_grid_node(
                host=request.host,
                agent_port=request.agent_port,
                node_id=request.node_id,
                target_run_id=request.target_run_id,
            )
        except Exception as exc:
            logger.warning(
                "grid_node_reregister_dispatch_failed node_id=%s host_id=%s error=%s",
                request.node_id,
                request.host_id,
                exc,
            )
            metrics_recorders.GRID_NODE_RUN_ID_RECONCILE_FAILURES.inc()
            continue

        device = await device_locking.lock_device(db, request.device_id, load_sessions=False)
        if device.appium_node is not None:
            device.appium_node.grid_run_id = observed
            await db.commit()
            metrics_recorders.GRID_NODE_RUN_ID_CONVERGED.inc()
            dispatched += 1
        else:
            await db.rollback()

    return dispatched


async def _load_diverging_requests(db: AsyncSession) -> list[GridNodeReregisterRequest]:
    stmt = (
        select(
            AppiumNode.device_id,
            Device.host_id,
            Host.ip,
            Host.agent_port,
            AppiumNode.id,
            AppiumNode.desired_grid_run_id,
        )
        .join(Device, Device.id == AppiumNode.device_id)
        .join(Host, Host.id == Device.host_id)
        .where(Host.status == HostStatus.online)
        .where(AppiumNode.desired_grid_run_id.is_distinct_from(AppiumNode.grid_run_id))
        .order_by(AppiumNode.id)
    )
    rows = (await db.execute(stmt)).all()
    return [
        GridNodeReregisterRequest(
            device_id=row.device_id,
            host_id=row.host_id,
            host=row.ip,
            agent_port=row.agent_port,
            node_id=row.id,
            target_run_id=row.desired_grid_run_id,
        )
        for row in rows
    ]
