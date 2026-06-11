from __future__ import annotations

from prometheus_client import REGISTRY

from app.core.metrics_recorders import record_http_request

LABELS = {"method": "GET", "path": "/api/events", "status": "200"}


def _count(name: str) -> float:
    return REGISTRY.get_sample_value(name, LABELS) or 0.0


def test_duration_exempt_request_counts_but_skips_histogram() -> None:
    requests_before = _count("http_requests_total")
    duration_before = _count("http_request_duration_seconds_count")

    record_http_request("GET", "/api/events", 200, 300.0, include_duration=False)

    assert _count("http_requests_total") == requests_before + 1
    assert _count("http_request_duration_seconds_count") == duration_before


def test_default_still_records_duration() -> None:
    labels = {"method": "GET", "path": "/api/metrics-test", "status": "200"}
    before = REGISTRY.get_sample_value("http_request_duration_seconds_count", labels) or 0.0
    record_http_request("GET", "/api/metrics-test", 200, 0.05)
    assert REGISTRY.get_sample_value("http_request_duration_seconds_count", labels) == before + 1
