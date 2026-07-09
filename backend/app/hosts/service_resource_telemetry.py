from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, text

from app.core.coerce import coerce_float as _coerce_float
from app.core.leader import state_store as control_plane_state_store
from app.core.observability import get_logger
from app.core.timeutil import now_utc, parse_iso
from app.hosts.models import Host, HostResourceSample, HostStatus
from app.hosts.schemas import HostResourceSampleRead, HostResourceTelemetryResponse
from app.hosts.service_status_push import HOST_STATUS_NAMESPACE

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.engine import Row
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader

logger = get_logger(__name__)

# Largest accepted telemetry bucket size: one day, expressed in minutes.
_MAX_BUCKET_MINUTES = 1440


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        return round(value)
    return None


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


class HostResourceTelemetryService:
    def __init__(
        self,
        *,
        settings: SettingsReader,
    ) -> None:
        self._settings = settings

    async def apply_host_resource_sample(
        self,
        db: AsyncSession,
        host: Host,
        sample: dict[str, Any],
    ) -> HostResourceSample:
        recorded_at = parse_iso(sample.get("recorded_at")) or now_utc()
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

    async def poll_once(self, db: AsyncSession) -> None:
        result = await db.execute(select(Host).where(Host.status == HostStatus.online).order_by(Host.hostname))
        hosts = result.scalars().all()

        for host in hosts:
            try:
                raw = await control_plane_state_store.get_value(db, HOST_STATUS_NAMESPACE, str(host.id))
                payload = raw.get("payload") if isinstance(raw, dict) else None
                sample = payload.get("host_telemetry") if isinstance(payload, dict) else None
                if not isinstance(sample, dict):
                    continue
                recorded_at = parse_iso(sample.get("recorded_at"))
                latest = await db.scalar(
                    select(func.max(HostResourceSample.recorded_at)).where(HostResourceSample.host_id == host.id)
                )
                if recorded_at is not None and latest is not None and recorded_at <= latest:
                    continue  # agent stopped pushing — don't duplicate the stale sample
                await self.apply_host_resource_sample(db, host, sample)
                await db.commit()
            except Exception:
                await db.rollback()
                logger.exception("Unexpected host resource telemetry failure for host %s", host.hostname)

    async def fetch_host_resource_telemetry(
        self,
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

        retention_hours = self._settings.get_int("retention.host_resource_telemetry_hours")
        if since >= until:
            raise ValueError("since must be earlier than until")
        if not 1 <= bucket_minutes <= _MAX_BUCKET_MINUTES:
            raise ValueError(f"bucket_minutes must be between 1 and {_MAX_BUCKET_MINUTES}")
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
