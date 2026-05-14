"""Telemetry factories — host resource samples + analytics capacity snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.analytics.models import AnalyticsCapacitySnapshot
from app.models.host_resource_sample import HostResourceSample
from app.seeding.time_patterns import weekly_activity_multiplier

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterator

    from app.seeding.context import SeedContext


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def host_resource_series(
    ctx: SeedContext,
    *,
    host_id: uuid.UUID,
    days_back: int,
    memory_total_mb: int = 16_384,
    disk_total_gb: float = 500.0,
) -> Iterator[HostResourceSample]:
    """Yield samples at 1-min/5-min/1-hour resolution bands."""
    end = ctx.now

    def _sample(ts: datetime) -> HostResourceSample:
        base_cpu = 10 + 20 * weekly_activity_multiplier(ts) / 2.5
        cpu = _clamp(base_cpu + ctx.rng.gauss(0, 6), 0, 100)
        memory_used = int(
            _clamp(
                memory_total_mb * (0.35 + 0.1 * weekly_activity_multiplier(ts) / 2.5),
                0,
                memory_total_mb,
            )
        )
        disk_used = _clamp(disk_total_gb * 0.62 + ctx.rng.gauss(0, 3), 0, disk_total_gb)
        return HostResourceSample(
            host_id=host_id,
            recorded_at=ts,
            cpu_percent=round(cpu, 1),
            memory_used_mb=memory_used,
            memory_total_mb=memory_total_mb,
            disk_used_gb=round(disk_used, 1),
            disk_total_gb=disk_total_gb,
            disk_percent=round(disk_used / disk_total_gb * 100, 1),
        )

    # 1-min resolution, last 24h
    ts = end - timedelta(hours=24)
    while ts <= end:
        yield _sample(ts)
        ts += timedelta(minutes=1)

    # 5-min resolution, day -8..-2
    ts = end - timedelta(days=8)
    stop = end - timedelta(hours=24)
    while ts < stop:
        yield _sample(ts)
        ts += timedelta(minutes=5)

    # 1-hour resolution, day -days_back..-8
    ts = end - timedelta(days=days_back)
    stop = end - timedelta(days=8)
    while ts < stop:
        yield _sample(ts)
        ts += timedelta(hours=1)


def make_capacity_snapshot(
    ctx: SeedContext,
    *,
    captured_at: datetime,
    total_capacity_slots: int,
    active_sessions: int,
    queued_requests: int,
    hosts_total: int,
    hosts_online: int,
    devices_total: int,
    devices_available: int,
) -> AnalyticsCapacitySnapshot:
    del ctx  # unused; present for uniform factory signature
    return AnalyticsCapacitySnapshot(
        captured_at=captured_at,
        total_capacity_slots=total_capacity_slots,
        active_sessions=active_sessions,
        queued_requests=queued_requests,
        available_capacity_slots=total_capacity_slots - active_sessions - queued_requests,
        hosts_total=hosts_total,
        hosts_online=hosts_online,
        devices_total=devices_total,
        devices_available=devices_available,
    )
