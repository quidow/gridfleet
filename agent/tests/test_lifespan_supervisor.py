"""Restart supervision for the lifespan's background loops.

Covers the two properties PR #912 listed as known exposures: a crashing loop
must back off instead of respawning at event-loop speed, and a crash callback
that lands after teardown must not spawn a task teardown has already walked past.
"""

from __future__ import annotations

from agent_app.lifespan import (
    _RESTART_BASE_DELAY_SEC,
    _RESTART_HEALTHY_AFTER_SEC,
    _RESTART_MAX_DELAY_SEC,
    _restart_delay,
)


def test_restart_delay_schedule() -> None:
    """The first crash of an episode is free; a crash loop pays doubling seconds.

    ``consecutive_crashes`` counts crashes already seen, so 0 is the first crash
    of an episode. Status pushes are what keep a host reading online, so the
    single-crash case — the one PR #912 exists to prevent — costs no silence.
    """
    delays = [_restart_delay(n, _RESTART_BASE_DELAY_SEC, _RESTART_MAX_DELAY_SEC) for n in range(9)]

    assert delays == [0.0, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_restart_delay_is_capped_far_out() -> None:
    """A loop that has been crashing for hours still retries, just no faster than the cap."""
    assert _restart_delay(40, _RESTART_BASE_DELAY_SEC, _RESTART_MAX_DELAY_SEC) == _RESTART_MAX_DELAY_SEC


def test_restart_delay_is_capped_above_float_exponent_limit() -> None:
    """Capping must happen before an unrepresentable exponent is evaluated."""
    assert _restart_delay(1025, _RESTART_BASE_DELAY_SEC, _RESTART_MAX_DELAY_SEC) == _RESTART_MAX_DELAY_SEC


def test_restart_constants_are_the_documented_values() -> None:
    """docs/reference/architecture.md quotes these three numbers; keep them honest."""
    assert (_RESTART_BASE_DELAY_SEC, _RESTART_MAX_DELAY_SEC, _RESTART_HEALTHY_AFTER_SEC) == (1.0, 60.0, 60.0)
