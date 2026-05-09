from __future__ import annotations

from app.metrics_recorders import HEARTBEAT_PING_TOTAL, record_heartbeat_ping


def test_heartbeat_ping_metric_increments_with_labels() -> None:
    sample_before = HEARTBEAT_PING_TOTAL.labels(host_id="hid", outcome="success", client_mode="pooled")._value.get()  # type: ignore[attr-defined]
    record_heartbeat_ping(
        host_id="hid",
        outcome="success",
        client_mode="pooled",
        duration_seconds=0.012,
    )
    sample_after = HEARTBEAT_PING_TOTAL.labels(host_id="hid", outcome="success", client_mode="pooled")._value.get()  # type: ignore[attr-defined]
    assert sample_after == sample_before + 1


def test_heartbeat_ping_helper_exported_via_app_metrics() -> None:
    from app.metrics import record_heartbeat_ping as exported

    assert exported is record_heartbeat_ping
