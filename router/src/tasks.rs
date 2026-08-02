//! Background maintenance loops. Both must be spawned from within a tokio
//! runtime context. Neither ever panics: backend errors are logged and the
//! loop continues so a flaky backend never takes the data plane down.

use std::sync::Arc;
use std::time::Duration;

use crate::activity::ActivityTracker;
use crate::backend::BackendClient;
use crate::metrics::metrics;
use crate::routes::{RouteMap, Upstream};

// The activity-flush cadence and its backend pair, checked at compile time.
// BACKEND_ACTIVITY_FRESH_WINDOW_SECS mirrors ACTIVITY_FRESH_WINDOW_SEC in
// backend/app/sessions/service_sync.py (owner: router — retune the cadence
// here first, then the backend window and this mirror). See the
// timeout-lattice table in docs/reference/architecture.md.
const ACTIVITY_FLUSH_CADENCE_SECS: u64 = 10;
const BACKEND_ACTIVITY_FRESH_WINDOW_SECS: u64 = 30;
// The backend sizes its session-freshness window as exactly 3x this cadence.
const _: () = assert!(BACKEND_ACTIVITY_FRESH_WINDOW_SECS == 3 * ACTIVITY_FLUSH_CADENCE_SECS);

/// Route reconcile cadence. A fixed plumbing constant, not a setting.
const ROUTE_RECONCILE_CADENCE_SECS: u64 = 1;

/// Every second: rebuild the route map from backend truth (bounds out-of-band
/// session-end staleness while preserving generation-protected data-plane
/// inserts). The immediate first `interval` tick is consumed before the loop
/// so the first rebuild happens after one full period — the data plane
/// already rebuilds lazily on cache miss, and this keeps e2e route-fetch
/// counts deterministic (no surprise startup fetch).
pub fn spawn_route_reconcile(routes: Arc<RouteMap>, backend: Arc<BackendClient>) {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(ROUTE_RECONCILE_CADENCE_SECS));
        interval.tick().await; // consume the immediate tick
        loop {
            interval.tick().await;
            // Capture the insert generation BEFORE the fetch: a session
            // inserted by the data plane mid-fetch must survive the
            // rebuild, not be evicted by the (now-stale) snapshot (C4).
            let gen = routes.insert_generation();
            match backend.fetch_routes().await {
                Ok(entries) => {
                    routes.replace_if_unchanged(
                        entries
                            .iter()
                            .filter_map(|(s, t)| Some((s.clone(), Upstream::parse(t)?)))
                            .collect(),
                        gen,
                    );
                    metrics().active_routes.set(routes.len() as i64);
                }
                Err(e) => log::warn!("route reconcile fetch failed: {e}"),
            }
        }
    });
}

/// Every 10s: drain the activity tracker and flush to the backend (batched,
/// never per-command). Skips the call entirely when nothing has been touched.
/// The backend liveness sweep sizes its activity-freshness window as 3x this
/// cadence (service_sync.py, ACTIVITY_FRESH_WINDOW_SEC) — the ratio is
/// compile-time asserted above.
pub fn spawn_activity_flush(activity: Arc<ActivityTracker>, backend: Arc<BackendClient>) {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(ACTIVITY_FLUSH_CADENCE_SECS));
        interval.tick().await; // consume the immediate tick
        loop {
            interval.tick().await;
            let drained = activity.drain();
            if drained.is_empty() {
                continue;
            }
            // Clone before the fallible flush: on failure we re-insert the
            // timestamps so an abandoned-but-still-active session is not falsely
            // aged out by the backend idle reaper. A newer touch that landed
            // mid-flush wins (see ActivityTracker::restore).
            if let Err(e) = backend.flush_activity(drained.clone()).await {
                log::warn!(
                    "activity flush failed, restoring {} entries: {e}",
                    drained.len()
                );
                activity.restore(drained);
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use std::thread;

    use super::*;

    /// Serves one request on `/internal/grid/routes` with `body`, on a real OS
    /// thread — actual network I/O, independent of tokio's paused virtual clock.
    fn stub_routes_backend(body: &'static str) -> Arc<BackendClient> {
        let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
        let addr = format!("http://{}", server.server_addr());
        thread::spawn(move || {
            let req = server.recv().unwrap();
            let resp = tiny_http::Response::from_string(body).with_header(
                tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
                    .unwrap(),
            );
            req.respond(resp).unwrap();
        });
        Arc::new(BackendClient::new(&addr, None))
    }

    /// Drives the paused-time runtime past one reconcile tick, then retries
    /// `done` against real wall-clock sleeps (up to ~3s total) until it
    /// reports the reconcile has landed.
    ///
    /// Two real-world gaps a bare `advance().await` does not cover:
    /// - A freshly-spawned task must register its pending tick against the
    ///   CURRENT instant before the clock moves: `time::advance` bumps the
    ///   clock synchronously before an unpolled task ever gets to construct
    ///   its `interval` (anchored on `Instant::now()`), so without a yield
    ///   first the task would anchor itself at the already-advanced instant
    ///   and never observe the tick.
    /// - The fetch itself is real socket I/O against `stub_routes_backend`'s
    ///   background OS thread, not virtual time — the paused clock does not
    ///   drive it, so the runtime thread needs actual wall-clock time to let
    ///   that thread run and the response arrive. Under load (the full test
    ///   binary running concurrently with others) that can take a moment, so
    ///   this polls instead of trusting a single fixed delay.
    async fn advance_one_tick(done: impl Fn() -> bool) {
        tokio::task::yield_now().await;
        tokio::time::advance(Duration::from_secs(1)).await;
        for _ in 0..600 {
            if done() {
                return;
            }
            thread::sleep(Duration::from_millis(5));
            tokio::task::yield_now().await;
        }
    }

    #[tokio::test(start_paused = true)]
    async fn stale_route_absent_from_backend_snapshot_is_dropped_after_one_tick() {
        let backend = stub_routes_backend(r#"{"routes":[]}"#);
        let routes = Arc::new(RouteMap::default());
        routes.insert(
            "stale",
            Upstream {
                host: "127.0.0.1".into(),
                port: 4723,
            },
        );

        spawn_route_reconcile(routes.clone(), backend);
        advance_one_tick(|| routes.is_empty()).await;

        assert!(routes.is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn route_still_in_backend_snapshot_survives_the_tick() {
        // "stale" is absent from the backend snapshot (like the other test) so a
        // reconcile that never ran cannot make this test pass vacuously: "kept"
        // surviving is only meaningful alongside "stale" actually being dropped.
        let backend = stub_routes_backend(
            r#"{"routes":[{"session_id":"kept","target":"http://127.0.0.1:4723"}]}"#,
        );
        let routes = Arc::new(RouteMap::default());
        routes.insert(
            "kept",
            Upstream {
                host: "127.0.0.1".into(),
                port: 4723,
            },
        );
        routes.insert(
            "stale",
            Upstream {
                host: "127.0.0.1".into(),
                port: 4723,
            },
        );

        spawn_route_reconcile(routes.clone(), backend);
        advance_one_tick(|| routes.get("stale").is_none()).await;

        assert!(routes.get("kept").is_some());
        assert!(routes.get("stale").is_none());
    }
}
