"""Pure helpers for demo data distributions.

All randomness flows through an injected `random.Random`. All timestamps are
timezone-aware UTC. No module-level state.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import random

# Workday multiplier table: hour 0..23 → intensity relative to a weekday trough.
_HOUR_WEIGHT: tuple[float, ...] = (
    0.1,
    0.1,
    0.1,
    0.1,
    0.15,
    0.25,  # 00..05
    0.4,
    0.7,
    1.1,
    1.8,
    2.3,
    2.5,  # 06..11
    2.4,
    2.2,
    2.4,
    2.5,
    2.3,
    1.9,  # 12..17
    1.4,
    1.0,
    0.8,
    0.6,
    0.4,
    0.2,  # 18..23
)
_WEEKEND_DAMPEN = 0.3


def weekly_activity_multiplier(ts: datetime) -> float:
    """Return a relative intensity for the given timestamp."""
    base = _HOUR_WEIGHT[ts.hour]
    if ts.weekday() >= 5:  # Sat/Sun
        base *= _WEEKEND_DAMPEN
    return base


def sample_run_timestamps(
    *,
    rng: random.Random,
    now: datetime,
    days_back: int,
    target_total: int,
    outage_window: tuple[datetime, datetime] | None = None,
) -> list[datetime]:
    """Sample `target_total` timestamps over the last `days_back` days.

    Samples by thinning: candidates are uniform, accepted with probability
    proportional to `weekly_activity_multiplier`. Candidates falling inside
    `outage_window` are rejected.
    """
    start = now - timedelta(days=days_back)
    max_weight = max(_HOUR_WEIGHT)
    accepted: list[datetime] = []
    attempts = 0
    max_attempts = target_total * 40  # safety bound
    while len(accepted) < target_total and attempts < max_attempts:
        attempts += 1
        seconds_offset = rng.uniform(0, days_back * 24 * 3600)
        candidate = start + timedelta(seconds=seconds_offset)
        if outage_window and outage_window[0] <= candidate <= outage_window[1]:
            continue
        weight = weekly_activity_multiplier(candidate)
        if rng.random() < weight / max_weight:
            accepted.append(candidate)
    accepted.sort()
    return accepted


def log_normal_duration_seconds(rng: random.Random) -> float:
    """Return a log-normal run duration clamped to [120s, 4h]."""
    mu = math.log(600.0)
    sigma = 1.0
    value = rng.lognormvariate(mu, sigma)
    return max(120.0, min(4 * 3600.0, value))
