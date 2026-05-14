from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from app.hosts import service_resource_telemetry as host_resource_telemetry
from app.hosts.models import HostResourceSample
from app.settings import settings_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


async def test_apply_host_resource_sample_persists_partial_fields(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    sample = {
        "recorded_at": "2026-04-16T09:30:00+00:00",
        "cpu_percent": 71.2,
        "memory_used_mb": 24576,
        "memory_total_mb": 32768,
        "disk_used_gb": None,
        "disk_total_gb": 512.0,
        "disk_percent": None,
    }

    row = await host_resource_telemetry.apply_host_resource_sample(db_session, db_host, sample)
    await db_session.commit()
    await db_session.refresh(row)

    assert row.host_id == db_host.id
    assert row.recorded_at == datetime(2026, 4, 16, 9, 30, tzinfo=UTC)
    assert row.cpu_percent == pytest.approx(71.2)
    assert row.memory_used_mb == 24576
    assert row.memory_total_mb == 32768
    assert row.disk_used_gb is None
    assert row.disk_total_gb == pytest.approx(512.0)
    assert row.disk_percent is None


async def test_fetch_host_resource_telemetry_buckets_samples(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    base = datetime(2026, 4, 16, 9, 0, tzinfo=UTC)
    db_session.add_all(
        [
            HostResourceSample(
                host_id=db_host.id,
                recorded_at=base + timedelta(minutes=index),
                cpu_percent=float(index),
                memory_used_mb=1000 + index,
                memory_total_mb=2000,
                disk_used_gb=20.0 + index,
                disk_total_gb=100.0,
                disk_percent=40.0 + index,
            )
            for index in range(30)
        ]
    )
    await db_session.commit()

    payload = await host_resource_telemetry.fetch_host_resource_telemetry(
        db_session,
        db_host.id,
        since=base,
        until=base + timedelta(minutes=30),
        bucket_minutes=5,
    )

    assert payload is not None
    assert len(payload.samples) == 6
    assert payload.samples[0].timestamp == base
    assert payload.samples[0].cpu_percent == pytest.approx(2.0)
    assert payload.samples[0].memory_used_mb == 1002
    assert payload.samples[5].timestamp == base + timedelta(minutes=25)
    assert payload.samples[5].cpu_percent == pytest.approx(27.0)
    assert payload.latest_recorded_at == base + timedelta(minutes=29)


async def test_fetch_host_resource_telemetry_omits_empty_buckets(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    base = datetime(2026, 4, 16, 9, 0, tzinfo=UTC)
    db_session.add_all(
        [
            HostResourceSample(
                host_id=db_host.id,
                recorded_at=base + timedelta(minutes=minute),
                cpu_percent=30.0 + minute,
                memory_used_mb=4000 + minute,
                memory_total_mb=8000,
                disk_used_gb=100.0,
                disk_total_gb=250.0,
                disk_percent=40.0,
            )
            for minute in (0, 1, 2, 10, 11, 12)
        ]
    )
    await db_session.commit()

    payload = await host_resource_telemetry.fetch_host_resource_telemetry(
        db_session,
        db_host.id,
        since=base,
        until=base + timedelta(minutes=15),
        bucket_minutes=5,
    )

    assert payload is not None
    assert [sample.timestamp for sample in payload.samples] == [
        base,
        base + timedelta(minutes=10),
    ]


@pytest.mark.parametrize(
    ("since", "until", "bucket_minutes"),
    [
        (
            datetime(2026, 4, 16, 9, 0, tzinfo=UTC),
            datetime(2026, 4, 16, 9, 0, tzinfo=UTC),
            5,
        ),
        (
            datetime(2026, 4, 16, 9, 0, tzinfo=UTC),
            datetime(2026, 4, 16, 10, 0, tzinfo=UTC),
            0,
        ),
        (
            datetime(2026, 4, 16, 9, 0, tzinfo=UTC),
            datetime(2026, 4, 16, 10, 0, tzinfo=UTC),
            1441,
        ),
    ],
)
async def test_fetch_host_resource_telemetry_validates_window_and_bucket(
    db_session: AsyncSession,
    db_host: Host,
    since: datetime,
    until: datetime,
    bucket_minutes: int,
) -> None:
    with pytest.raises(ValueError):
        await host_resource_telemetry.fetch_host_resource_telemetry(
            db_session,
            db_host.id,
            since=since,
            until=until,
            bucket_minutes=bucket_minutes,
        )


async def test_fetch_host_resource_telemetry_rejects_window_larger_than_retention(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    settings_service._cache["retention.host_resource_telemetry_hours"] = 1

    with pytest.raises(ValueError):
        await host_resource_telemetry.fetch_host_resource_telemetry(
            db_session,
            db_host.id,
            since=datetime(2026, 4, 16, 9, 0, tzinfo=UTC),
            until=datetime(2026, 4, 16, 11, 0, tzinfo=UTC),
            bucket_minutes=5,
        )
