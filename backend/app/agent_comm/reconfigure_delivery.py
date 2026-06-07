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

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher

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


def _row_matches_node_desired(row: AgentReconfigureOutbox, node: AppiumNode) -> bool:
    """True when the outbox row still reflects the node's current desired
    agent-visible config.

    ``node.generation`` advances on *any* reconcile field change (recovery
    flags, ``desired_port``, ``desired_state``), but the reconfigure payload only
    carries ``port`` / ``accepting_new_sessions`` / ``stop_pending`` /
    ``grid_run_id``. A pending row whose generation lags ``node.generation`` is
    therefore genuinely superseded only when one of these payload fields no
    longer matches — otherwise the lag is from an unrelated field change and the
    row must still be delivered. Treating every generation lag as stale silently
    drops the row (e.g. a run-id reconfigure staged just before a recovery-flag
    flip), so the node never learns its run id.
    """
    return (row.port, row.accepting_new_sessions, row.stop_pending, row.grid_run_id) == (
        node.port,
        node.accepting_new_sessions,
        node.stop_pending,
        node.desired_grid_run_id,
    )


async def deliver_agent_reconfigures(
    db: AsyncSession,
    device_id: object,
    *,
    limit: int = DELIVERY_BATCH_SIZE,
    agent_call_timeout: float | None = None,
    raise_on_failure: bool = False,
    settings: SettingsReader,
    circuit_breaker: CircuitBreakerProtocol,
    publisher: EventPublisher,
    pool: AgentHttpPool | None = None,
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
        if node is None or (row.reconciled_generation < node.generation and not _row_matches_node_desired(row, node)):
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
                    pool=pool,
                    circuit_breaker=circuit_breaker,
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
                    pool=pool,
                    circuit_breaker=circuit_breaker,
                )
        except (AgentUnreachableError, AgentResponseError) as exc:
            if isinstance(exc, AgentResponseError) and exc.http_status == 404:
                # The reconfigure route's only 404 is DEVICE_NOT_FOUND: the agent
                # authoritatively reports no managed Appium process on this port, so
                # the row can never be delivered — consume it (a future start carries
                # the node's current flags in its payload). And if the node row still
                # claims a process there, that observation is stale: the process died
                # outside a reconciler-issued stop (e.g. a maintenance graceful drain)
                # and only the appium_reconciler sweep — up to interval_sec (30s)
                # later — would clear it. The stale window retires start intents via
                # their node_running precondition and points probes at a dead port
                # (N11, 2026-06-07), so clear it now instead of waiting.
                metrics_recorders.AGENT_RECONFIGURE_OUTBOX_NO_PROCESS.inc()
                row.delivered_at = datetime.now(UTC)
                if node.pid is not None and node.port == row.port:
                    from app.appium_nodes.services.reconciler_agent import (  # noqa: PLC0415 — import cycle
                        mark_node_stopped,
                    )

                    # mark_node_stopped commits (including the row consumption above).
                    await mark_node_stopped(db, device, publisher=publisher)
                else:
                    await db.commit()
                continue
            _record_delivery_failure(row)
            await db.commit()
            if raise_on_failure:
                raise InlineReconfigureDeliveryFailedError(
                    f"agent reconfigure delivery failed for device {row.device_id} on port {row.port}",
                ) from exc
            continue
        row.delivered_at = datetime.now(UTC)
        await db.commit()


async def deliver_pending_agent_reconfigures(
    db: AsyncSession,
    *,
    limit: int = 100,
    settings: SettingsReader,
    circuit_breaker: CircuitBreakerProtocol,
    publisher: EventPublisher,
    pool: AgentHttpPool | None = None,
) -> None:
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
        await deliver_agent_reconfigures(
            db, device_id, settings=settings, circuit_breaker=circuit_breaker, publisher=publisher, pool=pool
        )


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
