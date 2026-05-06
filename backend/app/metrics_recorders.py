from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds.",
    labelnames=("method", "path", "status"),
)
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests handled by the backend.",
    labelnames=("method", "path", "status"),
)
AGENT_CALL_DURATION_SECONDS = Histogram(
    "agent_call_duration_seconds",
    "Agent HTTP call duration in seconds.",
    labelnames=("host", "endpoint"),
)
AGENT_CALLS_TOTAL = Counter(
    "agent_calls_total",
    "Total backend-to-agent HTTP calls.",
    labelnames=("host", "endpoint", "outcome"),
)
BACKGROUND_LOOP_DURATION_SECONDS = Histogram(
    "background_loop_duration_seconds",
    "Background loop iteration duration in seconds.",
    labelnames=("loop_name",),
)
BACKGROUND_LOOP_RUNS_TOTAL = Counter(
    "background_loop_runs_total",
    "Total successful background loop iterations.",
    labelnames=("loop_name",),
)
BACKGROUND_LOOP_ERRORS_TOTAL = Counter(
    "background_loop_errors_total",
    "Total failed background loop iterations.",
    labelnames=("loop_name",),
)
WEBHOOK_DELIVERIES_TOTAL = Counter(
    "webhook_deliveries_total",
    "Webhook delivery state transitions.",
    labelnames=("status",),
)
EVENTS_PUBLISHED_TOTAL = Counter(
    "events_published_total",
    "System events published by the backend.",
    labelnames=("event_type",),
)
RUN_CLAIMS_TOTAL = Counter(
    "gridfleet_run_claims_total",
    "Total successful claim_device responses, labeled by which include flags were requested.",
    labelnames=("include_config", "include_capabilities"),
)
ACTIVE_SSE_CONNECTIONS = Gauge(
    "active_sse_connections",
    "Number of active SSE subscribers.",
)
PENDING_JOBS = Gauge(
    "pending_jobs",
    "Number of pending durable jobs.",
)
ACTIVE_SESSIONS = Gauge(
    "active_sessions",
    "Number of active sessions in the backend database.",
)
DEVICES_IN_COOLDOWN = Gauge(
    "gridfleet_devices_in_cooldown",
    "Number of devices with an active run-scoped reservation cooldown.",
)


def record_http_request(method: str, path: str, status_code: int, duration_seconds: float) -> None:
    labels = {
        "method": method.upper(),
        "path": path,
        "status": str(status_code),
    }
    HTTP_REQUESTS_TOTAL.labels(**labels).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(**labels).observe(duration_seconds)


def record_agent_call(host: str, endpoint: str, outcome: str, duration_seconds: float) -> None:
    AGENT_CALLS_TOTAL.labels(host=host, endpoint=endpoint, outcome=outcome).inc()
    AGENT_CALL_DURATION_SECONDS.labels(host=host, endpoint=endpoint).observe(duration_seconds)


def record_background_loop_run(loop_name: str, duration_seconds: float) -> None:
    BACKGROUND_LOOP_RUNS_TOTAL.labels(loop_name=loop_name).inc()
    BACKGROUND_LOOP_DURATION_SECONDS.labels(loop_name=loop_name).observe(duration_seconds)


def record_background_loop_error(loop_name: str, duration_seconds: float) -> None:
    BACKGROUND_LOOP_ERRORS_TOTAL.labels(loop_name=loop_name).inc()
    BACKGROUND_LOOP_DURATION_SECONDS.labels(loop_name=loop_name).observe(duration_seconds)


def record_webhook_delivery(status: str, count: int = 1) -> None:
    if count <= 0:
        return
    WEBHOOK_DELIVERIES_TOTAL.labels(status=status).inc(count)


def record_event_published(event_type: str) -> None:
    EVENTS_PUBLISHED_TOTAL.labels(event_type=event_type).inc()
