from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import func, select, text

from app.agent_comm import operations as agent_operations
from app.core.database import async_session
from app.core.errors import AgentCallError
from app.core.observability import get_logger, observe_background_loop, parse_timestamp
from app.hosts.models import Host, HostResourceSample, HostStatus
from app.hosts.schemas import HostResourceSampleRead, HostResourceTelemetryResponse
from app.settings import settings_service

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.engine import Row
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)
LOOP_NAME = "host_resource_telemetry"
agent_host_telemetry = agent_operations.agent_host_telemetry


def _now() -> datetime:
    return datetime.now(UTC)


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        return round(value)
    return None


def _coerce_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return None


async def apply_host_resource_sample(
    db: AsyncSession,
    host: Host,
    sample: dict[str, Any],
) -> HostResourceSample:
    recorded_at = parse_timestamp(sample.get("recorded_at")) or _now()
    row = HostResourceSample(
        host_id=host.id,
        recorded_at=recorded_at,
        cpu_percent=_coerce_float(sample.get("cpu_percent")),
        memory_used_mb=_coerce_int(sample.get("memory_used_mb")),
        memory_total_mb=_coerce_int(sample.get("memory_total_mb")),
        disk_used_gb=_coerce_float(sample.get("disk_used_gb")),
        disk_total_gb=_coerce_float(sample.get("disk_total_gb")),
        disk_percent=_coerce_float(sample.get("disk_percent")),
    )
    db.add(row)
    await db.flush()
    return row


async def poll_host_resource_telemetry_once(db: AsyncSession) -> None:
    result = await db.execute(select(Host).where(Host.status == HostStatus.online).order_by(Host.hostname))
    hosts = result.scalars().all()

    for host in hosts:
        try:
            payload = await agent_host_telemetry(
                host.ip,
                host.agent_port,
                http_client_factory=httpx.AsyncClient,
            )
            if payload is None:
                continue
            await apply_host_resource_sample(db, host, payload)
            await db.commit()
        except AgentCallError as exc:
            await db.rollback()
            logger.warning("Host resource telemetry poll failed for host %s: %s", host.hostname, exc)
        except Exception:
            await db.rollback()
            logger.exception("Unexpected host resource telemetry failure for host %s", host.hostname)


def _window_exceeds_retention(*, since: datetime, until: datetime, retention_hours: int) -> bool:
    return until - since > timedelta(hours=retention_hours)


def _sample_from_row(row: Row[tuple[object, object, object, object, object, object, object]]) -> HostResourceSampleRead:
    return HostResourceSampleRead(
        timestamp=row[0],
        cpu_percent=_coerce_float(row[1]),
        memory_used_mb=_coerce_int(row[2]),
        memory_total_mb=_coerce_int(row[3]),
        disk_used_gb=_coerce_float(row[4]),
        disk_total_gb=_coerce_float(row[5]),
        disk_percent=_coerce_float(row[6]),
    )


async def fetch_host_resource_telemetry(
    db: AsyncSession,
    host_id: UUID,
    *,
    since: datetime,
    until: datetime,
    bucket_minutes: int,
) -> HostResourceTelemetryResponse | None:
    host_exists = await db.scalar(select(Host.id).where(Host.id == host_id))
    if host_exists is None:
        return None

    retention_hours = int(settings_service.get("retention.host_resource_telemetry_hours"))
    if since >= until:
        raise ValueError("since must be earlier than until")
    if not 1 <= bucket_minutes <= 1440:
        raise ValueError("bucket_minutes must be between 1 and 1440")
    if _window_exceeds_retention(since=since, until=until, retention_hours=retention_hours):
        raise ValueError("requested window exceeds retention.host_resource_telemetry_hours")

    bucket_query = text(
        """
        SELECT
            date_bin(
                make_interval(mins => CAST(:bucket_minutes AS integer)),
                recorded_at,
                :window_start
            ) AS bucket_start,
            AVG(cpu_percent) AS cpu_percent,
            AVG(memory_used_mb) AS memory_used_mb,
            AVG(memory_total_mb) AS memory_total_mb,
            AVG(disk_used_gb) AS disk_used_gb,
            AVG(disk_total_gb) AS disk_total_gb,
            AVG(disk_percent) AS disk_percent
        FROM host_resource_samples
        WHERE host_id = :host_id
          AND recorded_at >= :window_start
          AND recorded_at <= :window_end
        GROUP BY bucket_start
        ORDER BY bucket_start ASC
        """
    )
    rows = (
        await db.execute(
            bucket_query,
            {
                "bucket_minutes": bucket_minutes,
                "host_id": host_id,
                "window_start": since,
                "window_end": until,
            },
        )
    ).all()
    latest_recorded_at = await db.scalar(
        select(func.max(HostResourceSample.recorded_at)).where(HostResourceSample.host_id == host_id)
    )

    return HostResourceTelemetryResponse(
        samples=[_sample_from_row(row) for row in rows],
        latest_recorded_at=latest_recorded_at,
        window_start=since,
        window_end=until,
        bucket_minutes=bucket_minutes,
    )


async def host_resource_telemetry_loop() -> None:
    while True:
        interval = float(settings_service.get("general.host_resource_telemetry_interval_sec"))
        try:
            async with observe_background_loop(LOOP_NAME, interval).cycle(), async_session() as db:
                await poll_host_resource_telemetry_once(db)
        except Exception:
            logger.exception("Host resource telemetry loop failed")
        await asyncio.sleep(interval)
