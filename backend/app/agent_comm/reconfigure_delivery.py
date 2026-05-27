from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from app.agent_comm import operations as agent_operations
from app.agent_comm.models import AgentReconfigureOutbox
from app.appium_nodes.models import AppiumNode
from app.core import metrics_recorders
from app.core.errors import AgentResponseError, AgentUnreachableError
from app.devices.models import Device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader

DELIVERY_BATCH_SIZE = 5
MAX_DELIVERY_ATTEMPTS = 10
ABANDONED_REASON_MAX_ATTEMPTS = "max delivery attempts exceeded"
ABANDONED_REASON_HOST_MISSING = "host missing"

# Inline callers (e.g. cooldown HTTP handler) want fast failure on an
# unresponsive agent so the request does not stretch out to the default
# 10 s agent-call budget — testkit's cooldown timeout is also 10 s, so the
# combined latency could time the testkit-side call out. Background loop
# delivery keeps the original 10 s default.
INLINE_AGENT_CALL_TIMEOUT_SEC = 5.0


class InlineReconfigureDeliveryFailedError(Exception):
    """Raised by ``deliver_agent_reconfigures`` when an inline caller asked
    to be told about delivery failures.

    Background callers (loops) intentionally swallow failures and retry.
    Inline callers (e.g. the testkit ``cooldown_device`` HTTP handler) need
    a signal because the client treats a 200 response as "the agent applied
    the reconfigure" — proceeding to next steps under that assumption is
    unsafe if the drain never landed. Call sites attach the originating
    ``AgentUnreachableError`` / ``AgentResponseError`` via ``raise ... from
    exc`` so ``__cause__`` is set by the language; no custom field needed.
    """


async def deliver_agent_reconfigures(
    db: AsyncSession,
    device_id: object,
    *,
    limit: int = DELIVERY_BATCH_SIZE,
    agent_call_timeout: float | None = None,
    raise_on_failure: bool = False,
    settings: SettingsReader,
) -> None:
    await _mark_duplicate_generation_rows_delivered(db, device_id)
    metrics_recorders.AGENT_RECONFIGURE_OUTBOX_PENDING.set(
        int(
            await db.scalar(
                select(func.count())
                .select_from(AgentReconfigureOutbox)
                .where(
                    AgentReconfigureOutbox.delivered_at.is_(None),
                    AgentReconfigureOutbox.abandoned_at.is_(None),
                    AgentReconfigureOutbox.delivery_attempts < MAX_DELIVERY_ATTEMPTS,
                )
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
                    AgentReconfigureOutbox.abandoned_at.is_(None),
                    AgentReconfigureOutbox.delivery_attempts < MAX_DELIVERY_ATTEMPTS,
                )
                .order_by(AgentReconfigureOutbox.created_at)
                .limit(limit)
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
            _record_delivery_failure(row, abandoned_reason=ABANDONED_REASON_HOST_MISSING)
            await db.commit()
            continue
        try:
            if agent_call_timeout is None:
                await agent_operations.agent_appium_reconfigure(
                    device.host.ip,
                    device.host.agent_port,
                    port=row.port,
                    accepting_new_sessions=row.accepting_new_sessions,
                    stop_pending=row.stop_pending,
                    grid_run_id=row.grid_run_id,
                    settings=settings,
                )
            else:
                await agent_operations.agent_appium_reconfigure(
                    device.host.ip,
                    device.host.agent_port,
                    port=row.port,
                    accepting_new_sessions=row.accepting_new_sessions,
                    stop_pending=row.stop_pending,
                    grid_run_id=row.grid_run_id,
                    timeout=agent_call_timeout,
                    settings=settings,
                )
        except (AgentUnreachableError, AgentResponseError) as exc:
            _record_delivery_failure(row)
            await db.commit()
            if raise_on_failure:
                raise InlineReconfigureDeliveryFailedError(
                    f"agent reconfigure delivery failed for device {row.device_id} on port {row.port}",
                ) from exc
            continue
        row.delivered_at = datetime.now(UTC)
        await db.commit()


async def deliver_pending_agent_reconfigures(db: AsyncSession, *, limit: int = 100, settings: SettingsReader) -> None:
    device_ids = (
        (
            await db.execute(
                select(AgentReconfigureOutbox.device_id)
                .where(
                    AgentReconfigureOutbox.delivered_at.is_(None),
                    AgentReconfigureOutbox.abandoned_at.is_(None),
                    AgentReconfigureOutbox.delivery_attempts < MAX_DELIVERY_ATTEMPTS,
                )
                .group_by(AgentReconfigureOutbox.device_id)
                .order_by(func.min(AgentReconfigureOutbox.created_at))
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    for device_id in device_ids:
        await deliver_agent_reconfigures(db, device_id, settings=settings)


async def _mark_duplicate_generation_rows_delivered(db: AsyncSession, device_id: object) -> None:
    ranked = (
        select(
            AgentReconfigureOutbox.id.label("id"),
            func.row_number()
            .over(
                partition_by=AgentReconfigureOutbox.reconciled_generation,
                order_by=(AgentReconfigureOutbox.created_at.desc(), AgentReconfigureOutbox.id.desc()),
            )
            .label("rank"),
        )
        .where(
            AgentReconfigureOutbox.device_id == device_id,
            AgentReconfigureOutbox.delivered_at.is_(None),
            AgentReconfigureOutbox.abandoned_at.is_(None),
        )
        .subquery()
    )
    result = await db.execute(
        update(AgentReconfigureOutbox)
        .where(AgentReconfigureOutbox.id.in_(select(ranked.c.id).where(ranked.c.rank > 1)))
        .values(delivered_at=datetime.now(UTC))
    )
    if int(getattr(result, "rowcount", 0) or 0) > 0:
        await db.commit()


def _record_delivery_failure(
    row: AgentReconfigureOutbox,
    *,
    abandoned_reason: str = ABANDONED_REASON_MAX_ATTEMPTS,
) -> None:
    row.delivery_attempts += 1
    if row.delivery_attempts >= MAX_DELIVERY_ATTEMPTS:
        row.abandoned_at = datetime.now(UTC)
        row.abandoned_reason = abandoned_reason
        metrics_recorders.AGENT_RECONFIGURE_OUTBOX_ABANDONED.inc()
