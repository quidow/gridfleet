import random
from datetime import UTC, datetime, timedelta

from app.seeding.time_patterns import (
    log_normal_duration_seconds,
    sample_run_timestamps,
    weekly_activity_multiplier,
)


def test_weekly_multiplier_workday_peak_higher_than_weekend() -> None:
    wednesday_10am = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    saturday_10am = datetime(2026, 4, 18, 10, 0, tzinfo=UTC)
    assert weekly_activity_multiplier(wednesday_10am) > weekly_activity_multiplier(saturday_10am)


def test_weekly_multiplier_workday_night_lower_than_daytime() -> None:
    noon = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)
    three_am = datetime(2026, 4, 15, 3, 0, tzinfo=UTC)
    assert weekly_activity_multiplier(noon) > weekly_activity_multiplier(three_am)


def test_sample_run_timestamps_respects_density() -> None:
    rng = random.Random(0)
    now = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)
    stamps = sample_run_timestamps(
        rng=rng,
        now=now,
        days_back=90,
        target_total=500,
        outage_window=(now - timedelta(days=45, hours=10), now - timedelta(days=45, hours=-14)),
    )
    assert 450 <= len(stamps) <= 550
    assert all(s <= now for s in stamps)
    assert all(s >= now - timedelta(days=90) for s in stamps)
    outage_start = now - timedelta(days=45, hours=10)
    outage_end = now - timedelta(days=45, hours=-14)
    assert not any(outage_start < s < outage_end for s in stamps)


def test_log_normal_duration_clamped() -> None:
    rng = random.Random(1)
    for _ in range(2000):
        d = log_normal_duration_seconds(rng)
        assert 120 <= d <= 4 * 3600
