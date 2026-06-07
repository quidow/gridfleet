//! Prometheus exposition on the default registry.

use std::sync::OnceLock;

use prometheus::{Histogram, HistogramOpts, IntCounter, IntCounterVec, IntGauge, Opts};

pub struct Metrics {
    /// Commands seen, by class (new_session|command|delete|local).
    pub commands_total: IntCounterVec,
    /// Pre-resolved children of `commands_total` — the label set is the fixed
    /// route-class enum, so the hot path skips the per-request label-hash
    /// lookup of `with_label_values` (wave-5 #23).
    pub commands_new_session: IntCounter,
    pub commands_command: IntCounter,
    pub commands_delete: IntCounter,
    pub commands_local: IntCounter,
    /// Allocate-loop outcomes (allocated|queued|invalid|timeout|error).
    pub allocate_outcomes: IntCounterVec,
    /// Current number of live session routes.
    pub active_routes: IntGauge,
    /// DELETEs proxied that failed upstream before a response (route retained).
    pub delete_orphaned_total: IntCounter,
    /// New sessions created+confirmed but rolled back because the downstream
    /// client was gone by the time we tried to write the response.
    pub new_session_client_gone_total: IntCounter,
    /// Appium create returned 2xx but the body carried no sessionId; the
    /// allocation is failed and a best-effort session sweep runs on the target.
    pub create_missing_session_id_total: IntCounter,
    /// End-to-end request handling latency.
    pub request_duration: Histogram,
}

pub fn metrics() -> &'static Metrics {
    static METRICS: OnceLock<Metrics> = OnceLock::new();
    METRICS.get_or_init(|| {
        let commands_total = IntCounterVec::new(
            Opts::new(
                "gridfleet_router_commands_total",
                "WebDriver commands seen by class",
            ),
            &["class"],
        )
        .expect("metric");
        let allocate_outcomes = IntCounterVec::new(
            Opts::new(
                "gridfleet_router_allocate_outcomes_total",
                "New-session allocate outcomes",
            ),
            &["outcome"],
        )
        .expect("metric");
        let active_routes = IntGauge::new(
            "gridfleet_router_active_routes",
            "Current number of live session routes",
        )
        .expect("metric");
        let delete_orphaned_total = IntCounter::new(
            "gridfleet_router_delete_orphaned_total",
            "DELETE /session proxied but upstream failed before a response; route entry retained until the next reconcile.",
        )
        .expect("metric");
        let new_session_client_gone_total = IntCounter::new(
            "gridfleet_router_new_session_client_gone_total",
            "New sessions created and confirmed but rolled back (DELETE + session_ended) because the downstream client disconnected before the response could be written.",
        )
        .expect("metric");
        let create_missing_session_id_total = IntCounter::new(
            "gridfleet_router_create_missing_session_id_total",
            "Appium create returned a 2xx whose body had no sessionId; the allocation was failed and a best-effort session sweep ran on the target node.",
        )
        .expect("metric");
        let request_duration = Histogram::with_opts(HistogramOpts::new(
            "gridfleet_router_request_duration_seconds",
            "End-to-end request handling latency",
        ))
        .expect("metric");

        prometheus::register(Box::new(commands_total.clone())).expect("register");
        prometheus::register(Box::new(allocate_outcomes.clone())).expect("register");
        prometheus::register(Box::new(active_routes.clone())).expect("register");
        prometheus::register(Box::new(delete_orphaned_total.clone())).expect("register");
        prometheus::register(Box::new(new_session_client_gone_total.clone())).expect("register");
        prometheus::register(Box::new(create_missing_session_id_total.clone())).expect("register");
        prometheus::register(Box::new(request_duration.clone())).expect("register");

        let commands_new_session = commands_total.with_label_values(&["new_session"]);
        let commands_command = commands_total.with_label_values(&["command"]);
        let commands_delete = commands_total.with_label_values(&["delete"]);
        let commands_local = commands_total.with_label_values(&["local"]);

        Metrics {
            commands_total,
            commands_new_session,
            commands_command,
            commands_delete,
            commands_local,
            allocate_outcomes,
            active_routes,
            delete_orphaned_total,
            new_session_client_gone_total,
            create_missing_session_id_total,
            request_duration,
        }
    })
}

pub fn render() -> Vec<u8> {
    use prometheus::Encoder;
    let mut buf = Vec::new();
    prometheus::TextEncoder::new()
        .encode(&prometheus::gather(), &mut buf)
        .expect("encode");
    buf
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_registered_metrics() {
        metrics().commands_total.with_label_values(&["local"]).inc();
        metrics().active_routes.set(3);
        metrics().delete_orphaned_total.inc();
        metrics().new_session_client_gone_total.inc();
        metrics().create_missing_session_id_total.inc();
        let out = String::from_utf8(render()).unwrap();
        assert!(out.contains("gridfleet_router_commands_total"));
        assert!(out.contains("gridfleet_router_active_routes 3"));
        assert!(out.contains("gridfleet_router_delete_orphaned_total"));
        assert!(out.contains("gridfleet_router_new_session_client_gone_total"));
        assert!(out.contains("gridfleet_router_create_missing_session_id_total"));
    }
}
