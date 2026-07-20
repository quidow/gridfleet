from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.hosts import (
    service_resource_telemetry as host_resource_telemetry,
)
from app.hosts.service_resource_telemetry import HostResourceTelemetryService
from tests.fakes import FakeSettingsReader


class FlushSession:
    def __init__(self) -> None:
        self.flushed = False
        self.committed = False
        self.rolled_back = False
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flushed = True

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


async def test_host_resource_sample_coercion_and_apply() -> None:
    db = FlushSession()
    host = SimpleNamespace(id=uuid.uuid4())

    assert host_resource_telemetry._coerce_int(True) is None
    assert host_resource_telemetry._coerce_int(Decimal("12.6")) == 13
    assert host_resource_telemetry._coerce_float(False) is None
    assert host_resource_telemetry._coerce_float(Decimal("12.5")) == 12.5
    assert host_resource_telemetry._window_exceeds_retention(
        since=datetime(2026, 5, 1, tzinfo=UTC),
        until=datetime(2026, 5, 3, tzinfo=UTC),
        retention_hours=24,
    )
    sample = host_resource_telemetry._sample_from_row(
        (datetime(2026, 5, 1, tzinfo=UTC), Decimal("12.5"), Decimal("10.6"), 20, 1, 2, 50)
    )
    assert sample.memory_used_mb == 11

    row = await HostResourceTelemetryService(settings=FakeSettingsReader({})).apply_host_resource_sample(
        db,
        host,
        {
            "recorded_at": "2026-05-01T12:00:00Z",
            "cpu_percent": 12.5,
            "memory_used_mb": 1024.2,
            "memory_total_mb": 2048,
            "disk_used_gb": Decimal("10.5"),
            "disk_total_gb": 100,
            "disk_percent": 10,
        },
    )

    assert db.flushed is True
    assert row.host_id == host.id
    assert row.cpu_percent == 12.5
    assert row.memory_used_mb == 1024


async def test_fetch_host_resource_telemetry_validation_paths() -> None:
    host_id = uuid.uuid4()

    class FetchSession:
        async def scalar(self, *_args: object, **_kwargs: object) -> object | None:
            return host_id

    svc = HostResourceTelemetryService(
        settings=FakeSettingsReader({"retention.host_resource_telemetry_hours": 24}),
    )
    for since, until, bucket_minutes, message in (
        (
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 5, 1, tzinfo=UTC),
            5,
            "since must be earlier",
        ),
        (
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 5, 2, tzinfo=UTC),
            0,
            "bucket_minutes",
        ),
        (
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 5, 3, tzinfo=UTC),
            5,
            "retention",
        ),
    ):
        with pytest.raises(ValueError) as exc:
            await svc.fetch_host_resource_telemetry(
                FetchSession(),  # type: ignore[arg-type]
                host_id,
                since=since,
                until=until,
                bucket_minutes=bucket_minutes,
            )
        assert message in str(exc.value)


async def test_fetch_host_resource_telemetry_returns_none_for_missing_host() -> None:
    class MissingHostSession:
        async def scalar(self, *_args: object, **_kwargs: object) -> object | None:
            return None

    assert (
        await HostResourceTelemetryService(settings=FakeSettingsReader({})).fetch_host_resource_telemetry(
            MissingHostSession(),  # type: ignore[arg-type]
            uuid.uuid4(),
            since=datetime(2026, 5, 1, tzinfo=UTC),
            until=datetime(2026, 5, 2, tzinfo=UTC),
            bucket_minutes=5,
        )
        is None
    )
