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
    labelnames=("host", "endpoint", "client_mode"),
)
AGENT_CALLS_TOTAL = Counter(
    "agent_calls_total",
    "Total backend-to-agent HTTP calls.",
    labelnames=("host", "endpoint", "outcome", "client_mode"),
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
APPIUM_RECONCILER_ORPHANS_STOPPED = Counter(
    "appium_reconciler_orphans_stopped_total",
    "Agent appium processes stopped by the reconciler because no DB row claimed them.",
    labelnames=("reason",),
)
APPIUM_RECONCILER_CYCLE_FAILURES = Counter(
    "appium_reconciler_cycle_failures_total",
    "Reconciler cycles that raised before completing.",
)
APPIUM_RECONCILER_LAST_CYCLE_SECONDS = Gauge(
    "appium_reconciler_last_cycle_seconds",
    "Wall-clock duration of the most recent reconciler cycle.",
)
APPIUM_DESIRED_STATE_WRITES = Counter(
    "appium_desired_state_writes_total",
    "Total writes of AppiumNode.desired_state by Phase 3 writers.",
    labelnames=("caller", "target_state"),
)
APPIUM_DESIRED_GRID_RUN_ID_WRITES = Counter(
    "gridfleet_appium_desired_grid_run_id_writes_total",
    "Total writes of AppiumNode.desired_grid_run_id by the run-routing writer.",
    labelnames=("caller",),
)
APPIUM_TRANSITION_TOKEN_WRITES = Counter(
    "appium_transition_token_writes_total",
    "Total transition_token allocations by Phase 3 writers.",
    labelnames=("caller",),
)
APPIUM_TRANSITION_TOKEN_OVERRIDDEN = Counter(
    "appium_transition_token_overridden_total",
    "Counts every time one writer overrode another writer's pending transition_token.",
    labelnames=("losing_source", "winning_source"),
)
APPIUM_RECONCILER_CONVERGENCE_ACTIONS = Counter(
    "appium_reconciler_convergence_actions_total",
    "Convergence actions executed by the Appium reconciler.",
    labelnames=("action",),
)
APPIUM_RECONCILER_HOST_CYCLE_SECONDS = Histogram(
    "appium_reconciler_host_cycle_seconds",
    "Wall-clock duration of one host's Appium convergence cycle.",
    labelnames=("host_id",),
)
APPIUM_RECONCILER_ALLOCATION_COLLISIONS = Counter(
    "appium_reconciler_allocation_collisions_total",
    "Port allocation collisions encountered by the Appium reconciler.",
)
APPIUM_RECONCILER_TRANSITION_TOKEN_EXPIRED = Counter(
    "appium_reconciler_transition_token_expired_total",
    "Restart transition tokens cleared by the Appium reconciler after their deadline elapsed.",
)
APPIUM_RECONCILER_START_FAILURES = Counter(
    "appium_reconciler_start_failures_total",
    "Agent-start failures observed by the Appium reconciler, labeled by reason.",
    labelnames=("reason",),
)
APPIUM_RECONCILER_STOP_FAILURES = Counter(
    "appium_reconciler_stop_failures_total",
    "Agent-stop failures observed by the Appium reconciler, labeled by reason.",
    labelnames=("reason",),
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


def record_agent_call(
    *,
    host: str,
    endpoint: str,
    outcome: str,
    client_mode: str,
    duration_seconds: float,
) -> None:
    AGENT_CALLS_TOTAL.labels(host=host, endpoint=endpoint, outcome=outcome, client_mode=client_mode).inc()
    AGENT_CALL_DURATION_SECONDS.labels(host=host, endpoint=endpoint, client_mode=client_mode).observe(duration_seconds)


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


HEARTBEAT_PING_DURATION_SECONDS = Histogram(
    "gridfleet_agent_heartbeat_duration_seconds",
    "Backend->agent heartbeat ping duration in seconds.",
    labelnames=("host_id", "outcome", "client_mode"),
)
HEARTBEAT_PING_TOTAL = Counter(
    "gridfleet_agent_heartbeat_total",
    "Total backend->agent heartbeat pings.",
    labelnames=("host_id", "outcome", "client_mode"),
)
HEARTBEAT_CYCLE_DURATION_SECONDS = Histogram(
    "gridfleet_heartbeat_cycle_duration_seconds",
    "Wall clock duration of one full _check_hosts iteration.",
)
HEARTBEAT_CYCLE_OVERRUN_TOTAL = Counter(
    "gridfleet_heartbeat_cycle_overrun_total",
    "Heartbeat cycles whose duration exceeded heartbeat_interval_sec.",
)


def record_heartbeat_ping(
    *,
    host_id: str,
    outcome: str,
    client_mode: str,
    duration_seconds: float,
) -> None:
    HEARTBEAT_PING_TOTAL.labels(host_id=host_id, outcome=outcome, client_mode=client_mode).inc()
    HEARTBEAT_PING_DURATION_SECONDS.labels(host_id=host_id, outcome=outcome, client_mode=client_mode).observe(
        duration_seconds
    )


def record_heartbeat_cycle(duration_seconds: float, *, interval_seconds: float) -> None:
    HEARTBEAT_CYCLE_DURATION_SECONDS.observe(duration_seconds)
    if duration_seconds > interval_seconds:
        HEARTBEAT_CYCLE_OVERRUN_TOTAL.inc()


ip_ping_failures_total = Counter(
    "gridfleet_ip_ping_failures_total",
    "Total ICMP ping misses observed by the connectivity loop.",
    ["device_identity", "host"],
)

ip_ping_consecutive_failures = Gauge(
    "gridfleet_ip_ping_consecutive_failures",
    "Current consecutive ICMP ping miss counter, per device.",
    ["device_identity", "host"],
)


def record_ip_ping_failure(*, device_identity: str, host: str) -> None:
    ip_ping_failures_total.labels(device_identity=device_identity, host=host).inc()


def set_ip_ping_consecutive_failures(*, device_identity: str, host: str, value: int) -> None:
    ip_ping_consecutive_failures.labels(device_identity=device_identity, host=host).set(value)
