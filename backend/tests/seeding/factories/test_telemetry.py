from __future__ import annotations

import uuid
from datetime import timedelta

from app.seeding.context import SeedContext
from app.seeding.factories.telemetry import (
    host_resource_series,
    make_capacity_snapshot,
)


def test_host_resource_series_resolution_bands() -> None:
    ctx = SeedContext.build(session=None, seed=1)  # type: ignore[arg-type]
    host_id = uuid.uuid4()
    samples = list(host_resource_series(ctx, host_id=host_id, days_back=90))
    # 1-min x 24h + 5-min x 7d + 1h x 82d (inclusive endpoints may +/-1)
    expected_lo = 1440 + 7 * 288 + 82 * 24 - 5
    expected_hi = 1440 + 7 * 288 + 82 * 24 + 5
    assert expected_lo <= len(samples) <= expected_hi
    assert all(s.host_id == host_id for s in samples)
    assert all(0 <= (s.cpu_percent or 0) <= 100 for s in samples)


def test_make_capacity_snapshot_populates_counts() -> None:
    ctx = SeedContext.build(session=None, seed=1)  # type: ignore[arg-type]
    snap = make_capacity_snapshot(
        ctx,
        captured_at=ctx.now - timedelta(hours=1),
        total_capacity_slots=35,
        active_sessions=4,
        queued_requests=1,
        hosts_total=4,
        hosts_online=3,
        devices_total=35,
        devices_available=28,
    )
    assert snap.available_capacity_slots == 35 - 4 - 1
