"""Both wake-source label values must exist from process start so dashboards can
tell "zero doorbell wakes" from "doorbell not wired" — a counter label series
only appears after its first increment unless pre-registered."""

from __future__ import annotations

from prometheus_client import REGISTRY

import app.appium_nodes.services.node_health
import app.sessions.service_sync  # noqa: F401


def test_wake_source_labels_preregistered() -> None:
    for metric in ("gridfleet_node_health_wake_source_total", "gridfleet_session_sync_wake_source_total"):
        for source in ("doorbell", "tick"):
            assert REGISTRY.get_sample_value(metric, {"source": source}) is not None, f"{metric}{{{source}}}"
