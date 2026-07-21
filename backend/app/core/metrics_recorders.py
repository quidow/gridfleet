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
HTTP_UNHANDLED_EXCEPTIONS_TOTAL = Counter(
    "http_unhandled_exception_total",
    "Uncaught exceptions that reached the catch-all handler and returned HTTP 500.",
    # ``path`` is the templated route path (not the raw URL) to bound cardinality;
    # ``pgcode`` carries the Postgres SQLSTATE for DBAPIError, else empty string.
    labelnames=("path", "exc_type", "pgcode"),
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
BACKGROUND_LOOP_PHASE_DURATION_SECONDS = Histogram(
    "background_loop_phase_duration_seconds",
    "Duration of one named phase within a background loop iteration.",
    labelnames=("loop_name", "phase"),
    # Observation-loop cycles can far exceed the 10s ceiling of the default
    # buckets; extend the range so slow phases land in real buckets, not +Inf.
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 40.0, 80.0),
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
BACKGROUND_LOOP_OVERRUN_TOTAL = Counter(
    "background_loop_overrun_total",
    "Background loop cycles whose duration exceeded the loop's configured interval.",
    labelnames=("loop_name",),
)
BACKGROUND_LOOP_EFFECTIVE_PERIOD_SECONDS = Gauge(
    "background_loop_effective_period_seconds",
    "Wall-clock seconds between the start of one cycle and the next (cycle duration + inter-cycle sleep).",
    labelnames=("loop_name",),
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
APPIUM_PULL_MODE_SKIPPED_ACTIONS = Counter(
    "gridfleet_appium_pull_mode_skipped_actions_total",
    "Agent-touching convergence actions skipped in pull mode, labeled by the action kind; "
    "the agent owns the start/stop/restart transition and reports the result as observed facts.",
    labelnames=("kind",),
)
APPIUM_PULL_MODE_ORPHANS_OBSERVED = Counter(
    "gridfleet_appium_pull_mode_orphans_observed_total",
    "Orphan Appium node ports observed in pull mode with no matching desired row. "
    "Metric-only: the backend does not reap these directly.",
)
APPIUM_TERMINATE_FAILED_TOTAL = Counter(
    "appium_terminate_failed_total",
    "W3C session DELETE requests that failed at the transport layer (connect refused / timeout). "
    "A non-404 HTTP refusal is not counted here — it returns a response, not a transport error.",
)
FORCED_RELEASE_NODE_STOP_TOTAL = Counter(
    "gridfleet_forced_release_node_stop_total",
    "Force-release hard node-stops actually fired — i.e. the session survived (or stayed "
    "indeterminate after a retry) the W3C DELETE. Steady-state low: most force-releases DELETE cleanly.",
)
EVENTS_PUBLISHED_TOTAL = Counter(
    "events_published_total",
    "System events published by the backend.",
    labelnames=("event_type",),
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
INTENT_REGISTRY_INTENTS = Gauge(
    "intent_registry_intents_total",
    "Current number of registered device orchestration intents.",
)
INTENT_RECONCILER_EVALUATIONS = Counter(
    "intent_reconciler_evaluations_total",
    "Total device intent reconciliation evaluations.",
)


def record_http_request(
    method: str, path: str, status_code: int, duration_seconds: float, *, include_duration: bool = True
) -> None:
    labels = {
        "method": method.upper(),
        "path": path,
        "status": str(status_code),
    }
    HTTP_REQUESTS_TOTAL.labels(**labels).inc()
    if include_duration:
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


def record_background_loop_phase(loop_name: str, phase: str, duration_seconds: float) -> None:
    BACKGROUND_LOOP_PHASE_DURATION_SECONDS.labels(loop_name=loop_name, phase=phase).observe(duration_seconds)


def record_background_loop_error(loop_name: str, duration_seconds: float) -> None:
    BACKGROUND_LOOP_ERRORS_TOTAL.labels(loop_name=loop_name).inc()
    BACKGROUND_LOOP_DURATION_SECONDS.labels(loop_name=loop_name).observe(duration_seconds)


def record_background_loop_overrun(loop_name: str, duration_seconds: float, *, interval_seconds: float) -> None:
    """Count a cycle that missed its cadence (took longer than its interval).

    When ``interval_seconds <= 0`` (doorbell-woken loops with no fixed cadence), the cycle is never counted as an
    overrun.
    """
    if interval_seconds > 0 and duration_seconds > interval_seconds:
        BACKGROUND_LOOP_OVERRUN_TOTAL.labels(loop_name=loop_name).inc()


def record_background_loop_effective_period(loop_name: str, period_seconds: float) -> None:
    """Record the real cadence (cycle start to next cycle start) of a loop.

    Unlike ``background_loop_duration_seconds`` (cycle work only) this includes
    the inter-cycle sleep, so operators see the true period rather than the
    configured interval.
    """
    BACKGROUND_LOOP_EFFECTIVE_PERIOD_SECONDS.labels(loop_name=loop_name).set(period_seconds)


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


ip_ping_failures_total = Counter(
    "gridfleet_ip_ping_failures_total",
    "Total ICMP ping misses observed by the connectivity loop.",
    ["device_identity", "host"],
)

ip_ping_failing_seconds = Gauge(
    "gridfleet_ip_ping_failing_seconds",
    "Elapsed seconds in the current ICMP ping failure episode, per device.",
    ["device_identity", "host"],
)


def record_ip_ping_failure(*, device_identity: str, host: str) -> None:
    ip_ping_failures_total.labels(device_identity=device_identity, host=host).inc()


def set_ip_ping_failing_seconds(*, device_identity: str, host: str, value: float) -> None:
    ip_ping_failing_seconds.labels(device_identity=device_identity, host=host).set(value)


device_repair_attempts_total = Counter(
    "gridfleet_device_repair_attempts_total",
    "Total adapter-recommended repair dispatches by the connectivity loop.",
    ["action", "outcome"],
)


def record_device_repair_attempt(*, action: str, outcome: str) -> None:
    device_repair_attempts_total.labels(action=action, outcome=outcome).inc()


HOST_STATUS_PUSHES = Counter(
    "gridfleet_host_status_pushes_total",
    "Consolidated agent status pushes ingested",
    labelnames=("host_id",),
)
HOST_PUSH_OBSERVATION_FAILURES = Counter(
    "gridfleet_host_push_observation_failures_total",
    "Push-time observation stages that raised and were contained.",
    labelnames=("stage",),
)
# Boot-fence and dedup-token diagnostics on the status-push ingest path.
HOST_PUSH_BOOT_FENCE_REJECTIONS = Counter(
    "gridfleet_host_push_boot_fence_rejections_total",
    "Status pushes rejected because boot_id did not match the host's registered boot.",
)
HOST_PUSH_TOKEN_ANOMALIES = Counter(
    "gridfleet_host_push_token_anomalies_total",
    "Dedup-token anomalies on the two moved health sections.",
    # kind: same_sequence_different_hash | hash_mismatch | tokenless_after_boot
    labelnames=("kind",),
)


def record_host_status_push(*, host_id: str) -> None:
    HOST_STATUS_PUSHES.labels(host_id=host_id).inc()


def record_host_push_boot_fence_rejection() -> None:
    HOST_PUSH_BOOT_FENCE_REJECTIONS.inc()


def record_host_push_token_anomaly(kind: str) -> None:
    HOST_PUSH_TOKEN_ANOMALIES.labels(kind=kind).inc()


# StatusFoldLoop diagnostics: per-node fold outcomes and per-cycle bookkeeping.
STATUS_FOLD_NODE_RESULTS = Counter(
    "gridfleet_status_fold_node_results_total",
    "Per-node results of the async node_health fold.",
    # outcome: applied | terminal_noop | skipped | retryable
    labelnames=("outcome",),
)
STATUS_FOLD_HOSTS = Counter(
    "gridfleet_status_fold_hosts_total",
    "Host sections the StatusFoldLoop acted on, by disposition.",
    # disposition: folded | skipped | contained_error | budget_deferred
    labelnames=("disposition",),
)
STATUS_FOLD_LAG_SECONDS = Histogram(
    "gridfleet_status_fold_lag_seconds",
    "Snapshot-received-to-fold-complete lag for a folded host section.",
    buckets=(0.5, 1, 2, 3, 5, 8, 13, 21, 34),
)
STATUS_FOLD_OLDEST_UNAPPLIED_SECONDS = Gauge(
    "gridfleet_status_fold_oldest_unapplied_seconds",
    "Age of the oldest pushed section still awaiting a fold (slow-burn stall signal).",
)


def record_node_health_fold_result(outcome: str) -> None:
    STATUS_FOLD_NODE_RESULTS.labels(outcome=outcome).inc()


def record_status_fold_host(disposition: str) -> None:
    STATUS_FOLD_HOSTS.labels(disposition=disposition).inc()


def record_status_fold_lag(seconds: float) -> None:
    STATUS_FOLD_LAG_SECONDS.observe(seconds)


def record_status_fold_oldest_unapplied(seconds: float) -> None:
    STATUS_FOLD_OLDEST_UNAPPLIED_SECONDS.set(seconds)


STATUS_FOLD_DEVICE_RESULTS = Counter(
    "gridfleet_status_fold_device_results_total",
    "Device-health fold per-device outcomes on the StatusFoldLoop.",
    # outcome: applied | terminal_noop | skipped | retryable
    labelnames=("outcome",),
)


def record_device_health_fold_result(outcome: str) -> None:
    STATUS_FOLD_DEVICE_RESULTS.labels(outcome=outcome).inc()


DB_SERIALIZATION_RETRY_TOTAL = Counter(
    "db_serialization_retry_total",
    "Transactions rolled back and re-run after a transient Postgres deadlock/serialization failure (40P01/40001).",
    labelnames=("caller", "outcome"),
)

DB_POOL_SIZE = Gauge(
    "db_pool_size",
    "Configured SQLAlchemy connection pool size (base capacity, excludes overflow).",
)
DB_POOL_CHECKED_OUT = Gauge(
    "db_pool_checked_out",
    "Connections currently checked out of the SQLAlchemy pool.",
)
DB_POOL_OVERFLOW = Gauge(
    "db_pool_overflow",
    "Current pool overflow: connections opened beyond pool_size. "
    "Negative means spare base capacity (fewer connections open than pool_size).",
)
