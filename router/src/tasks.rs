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

/// Every 60s: rebuild the route map from backend truth (bounds staleness).
/// The immediate first `interval` tick is consumed before the loop so the
/// first rebuild happens after one full period — the data plane already
/// rebuilds lazily on cache miss, and this keeps e2e route-fetch counts
/// deterministic (no surprise startup fetch).
pub fn spawn_route_reconcile(routes: Arc<RouteMap>, backend: Arc<BackendClient>) {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(60));
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
