"""Legacy metrics surface for the GridFleet backend.

Phase 0a state: this module re-exports every Prometheus gauge and
recorder from :mod:`app.metrics_recorders` plus the fan-out dispatcher
from :mod:`app.core.metrics`, and carries the legacy cross-domain
aggregator as :func:`refresh_system_gauges_legacy`.
:func:`refresh_system_gauges` is kept as a module-level alias pointing
at the legacy aggregator so existing callers continue to import the
old name unchanged.

The migration cutover happens domain by domain (events P5, jobs P6,
devices P13, sessions P14). Each contributing-domain phase moves its
gauge update from :func:`refresh_system_gauges_legacy` into a callback
registered with :func:`app.core.metrics.register_gauge_refresher`. The
final contributor phase (P14, sessions) also flips ``app/main.py``'s
``/metrics`` handler to call :func:`app.core.metrics.refresh_system_gauges`
instead of this module's :func:`refresh_system_gauges_legacy`. Phase 16
deletes this file entirely.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.metrics import (
    GaugeRefresher,
    register_gauge_refresher,
)
from app.core.metrics import refresh_system_gauges as _core_refresh_system_gauges
from app.metrics_recorders import (
    ACTIVE_SESSIONS,
    ACTIVE_SSE_CONNECTIONS,
    AGENT_CALL_DURATION_SECONDS,
    AGENT_CALLS_TOTAL,
    BACKGROUND_LOOP_DURATION_SECONDS,
    BACKGROUND_LOOP_ERRORS_TOTAL,
    BACKGROUND_LOOP_RUNS_TOTAL,
    DEVICES_IN_COOLDOWN,
    EVENTS_PUBLISHED_TOTAL,
    HEARTBEAT_CYCLE_DURATION_SECONDS,
    HEARTBEAT_CYCLE_OVERRUN_TOTAL,
    HEARTBEAT_PING_DURATION_SECONDS,
    HEARTBEAT_PING_TOTAL,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
    PENDING_JOBS,
    WEBHOOK_DELIVERIES_TOTAL,
    ip_ping_consecutive_failures,
    ip_ping_failures_total,
    record_agent_call,
    record_background_loop_error,
    record_background_loop_run,
    record_event_published,
    record_heartbeat_cycle,
    record_heartbeat_ping,
    record_http_request,
    record_ip_ping_failure,
    record_webhook_delivery,
    set_ip_ping_consecutive_failures,
)


async def refresh_system_gauges_legacy(db: object) -> None:
    """Legacy cross-domain aggregator.

    Kept as a no-op compatibility symbol through Phase 16. The canonical
    gauge dispatcher is :func:`app.core.metrics.refresh_system_gauges`.
    """
    del db


# Backward-compat alias for callers still importing the pre-Phase-0a name.
refresh_system_gauges = _core_refresh_system_gauges


def render_metrics() -> bytes:
    return generate_latest()


__all__ = [
    "ACTIVE_SESSIONS",
    "ACTIVE_SSE_CONNECTIONS",
    "AGENT_CALLS_TOTAL",
    "AGENT_CALL_DURATION_SECONDS",
    "BACKGROUND_LOOP_DURATION_SECONDS",
    "BACKGROUND_LOOP_ERRORS_TOTAL",
    "BACKGROUND_LOOP_RUNS_TOTAL",
    "CONTENT_TYPE_LATEST",
    "DEVICES_IN_COOLDOWN",
    "EVENTS_PUBLISHED_TOTAL",
    "HEARTBEAT_CYCLE_DURATION_SECONDS",
    "HEARTBEAT_CYCLE_OVERRUN_TOTAL",
    "HEARTBEAT_PING_DURATION_SECONDS",
    "HEARTBEAT_PING_TOTAL",
    "HTTP_REQUESTS_TOTAL",
    "HTTP_REQUEST_DURATION_SECONDS",
    "PENDING_JOBS",
    "WEBHOOK_DELIVERIES_TOTAL",
    "GaugeRefresher",
    "ip_ping_consecutive_failures",
    "ip_ping_failures_total",
    "record_agent_call",
    "record_background_loop_error",
    "record_background_loop_run",
    "record_event_published",
    "record_heartbeat_cycle",
    "record_heartbeat_ping",
    "record_http_request",
    "record_ip_ping_failure",
    "record_webhook_delivery",
    "refresh_system_gauges",
    "refresh_system_gauges_legacy",
    "register_gauge_refresher",
    "render_metrics",
    "set_ip_ping_consecutive_failures",
]
