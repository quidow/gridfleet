from __future__ import annotations

from agent_app._supervision import ExponentialBackoff


def test_backoff_sequence_caps_at_configured_max() -> None:
    backoff = ExponentialBackoff(base=1.0, factor=2.0, cap=30.0, max_attempts=5, window_sec=300.0)
    assert [backoff.next_delay() for _ in range(7)] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_backoff_reset_starts_sequence_again() -> None:
    backoff = ExponentialBackoff(base=1.0, factor=2.0, cap=30.0, max_attempts=5, window_sec=300.0)
    assert backoff.next_delay() == 1.0
    assert backoff.next_delay() == 2.0
    backoff.reset()
    assert backoff.next_delay() == 1.0


def test_attempt_window_counts_recent_attempts() -> None:
    backoff = ExponentialBackoff(base=1.0, factor=2.0, cap=30.0, max_attempts=2, window_sec=10.0)
    backoff.record_attempt(100.0)
    backoff.record_attempt(105.0)
    backoff.record_attempt(112.0)
    assert backoff.attempts_in_window(112.0) == 2
    assert not backoff.can_attempt(112.0)
    assert backoff.can_attempt(116.0)
