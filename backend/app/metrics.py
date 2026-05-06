from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import column, func, select, table

from app.metrics_recorders import (
    ACTIVE_SESSIONS,
    ACTIVE_SSE_CONNECTIONS,
    AGENT_CALL_DURATION_SECONDS,
    AGENT_CALLS_TOTAL,
    BACKGROUND_LOOP_DURATION_SECONDS,
    BACKGROUND_LOOP_ERRORS_TOTAL,
    BACKGROUND_LOOP_RUNS_TOTAL,
    DEVICES_IN_COOLDOWN,
    EVENTS_PUBLISHED_TOTAL,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
    PENDING_JOBS,
    RUN_CLAIMS_TOTAL,
    WEBHOOK_DELIVERIES_TOTAL,
    record_agent_call,
    record_background_loop_error,
    record_background_loop_run,
    record_event_published,
    record_http_request,
    record_webhook_delivery,
)
from app.models.job import Job
from app.models.session import Session, SessionStatus
from app.services.event_bus import event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

DEVICE_RESERVATIONS = table(
    "device_reservations",
    column("device_id"),
    column("released_at"),
    column("excluded_until"),
)


async def refresh_system_gauges(db: AsyncSession) -> None:
    pending_jobs_result = await db.execute(select(func.count()).select_from(Job).where(Job.status == "pending"))
    active_sessions_result = await db.execute(
        select(func.count())
        .select_from(Session)
        .where(
            Session.status == SessionStatus.running,
            Session.ended_at.is_(None),
        )
    )
    PENDING_JOBS.set(int(pending_jobs_result.scalar_one()))
    ACTIVE_SESSIONS.set(int(active_sessions_result.scalar_one()))
    ACTIVE_SSE_CONNECTIONS.set(event_bus.subscriber_count)
    cooldown_result = await db.execute(
        select(func.count(func.distinct(DEVICE_RESERVATIONS.c.device_id)))
        .select_from(DEVICE_RESERVATIONS)
        .where(DEVICE_RESERVATIONS.c.released_at.is_(None))
        .where(DEVICE_RESERVATIONS.c.excluded_until.is_not(None))
        .where(DEVICE_RESERVATIONS.c.excluded_until > datetime.now(UTC))
    )
    DEVICES_IN_COOLDOWN.set(int(cooldown_result.scalar_one() or 0))


def render_metrics() -> bytes:
    return generate_latest()


__all__ = [
    "ACTIVE_SESSIONS",
    "ACTIVE_SSE_CONNECTIONS",
    "AGENT_CALLS_TOTAL",
    "AGENT_CALL_DURATION_SECONDS",
    "BACKGROUND_LOOP_DURATION_SECONDS",
    "BACKGROUND_LOOP_ERRORS_TOTAL",
    "BACKGROUND_LOOP_RUNS_TOTAL",
    "CONTENT_TYPE_LATEST",
    "DEVICES_IN_COOLDOWN",
    "EVENTS_PUBLISHED_TOTAL",
    "HTTP_REQUESTS_TOTAL",
    "HTTP_REQUEST_DURATION_SECONDS",
    "PENDING_JOBS",
    "RUN_CLAIMS_TOTAL",
    "WEBHOOK_DELIVERIES_TOTAL",
    "record_agent_call",
    "record_background_loop_error",
    "record_background_loop_run",
    "record_event_published",
    "record_http_request",
    "record_webhook_delivery",
    "refresh_system_gauges",
    "render_metrics",
]
