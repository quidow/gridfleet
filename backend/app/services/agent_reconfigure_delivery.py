from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app import metrics_recorders
from app.errors import AgentResponseError, AgentUnreachableError
from app.models.agent_reconfigure_outbox import AgentReconfigureOutbox
from app.models.appium_node import AppiumNode
from app.models.device import Device
from app.services import agent_operations

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def deliver_agent_reconfigures(db: AsyncSession, device_id: object) -> None:
    metrics_recorders.AGENT_RECONFIGURE_OUTBOX_PENDING.set(
        int(
            await db.scalar(
                select(func.count())
                .select_from(AgentReconfigureOutbox)
                .where(AgentReconfigureOutbox.delivered_at.is_(None))
            )
            or 0
        )
    )
    rows = (
        (
            await db.execute(
                select(AgentReconfigureOutbox)
                .where(
                    AgentReconfigureOutbox.device_id == device_id,
                    AgentReconfigureOutbox.delivered_at.is_(None),
                )
                .order_by(AgentReconfigureOutbox.created_at)
            )
        )
        .scalars()
        .all()
    )

    for row in rows:
        node = (await db.execute(select(AppiumNode).where(AppiumNode.device_id == row.device_id))).scalar_one_or_none()
        if node is None or row.reconciled_generation < node.generation:
            metrics_recorders.AGENT_RECONFIGURE_OUTBOX_STALE_SKIPPED.inc()
            row.delivered_at = datetime.now(UTC)
            await db.commit()
            continue

        device = (
            await db.execute(select(Device).where(Device.id == row.device_id).options(selectinload(Device.host)))
        ).scalar_one()
        if device.host is None:
            row.delivery_attempts += 1
            await db.commit()
            continue
        try:
            await agent_operations.agent_appium_reconfigure(
                device.host.ip,
                device.host.agent_port,
                port=row.port,
                accepting_new_sessions=row.accepting_new_sessions,
                stop_pending=row.stop_pending,
                grid_run_id=row.grid_run_id,
            )
        except (AgentUnreachableError, AgentResponseError):
            row.delivery_attempts += 1
            await db.commit()
            continue
        row.delivered_at = datetime.now(UTC)
        await db.commit()
