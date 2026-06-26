"""The session_sync wake-source label values must exist from process start so
dashboards can tell "zero doorbell wakes" from "doorbell not wired" — a counter
label series only appears after its first increment unless pre-registered."""

from __future__ import annotations

from prometheus_client import REGISTRY

import app.sessions.service_sync  # noqa: F401


def test_session_sync_wake_source_labels_preregistered() -> None:
    for source in ("doorbell", "tick"):
        assert REGISTRY.get_sample_value("gridfleet_session_sync_wake_source_total", {"source": source}) is not None, (
            source
        )
