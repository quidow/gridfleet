//! Background maintenance loops. Both must be spawned from within a tokio
//! runtime context. Neither ever panics: backend errors are logged and the
//! loop continues so a flaky backend never takes the data plane down.

use std::sync::Arc;
use std::time::Duration;

use crate::activity::ActivityTracker;
use crate::backend::BackendClient;
use crate::metrics::metrics;
use crate::routes::{RouteMap, Upstream};

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
            match backend.fetch_routes().await {
                Ok(entries) => {
                    routes.replace_all(
                        entries
                            .iter()
                            .filter_map(|(s, t)| Some((s.clone(), Upstream::parse(t)?)))
                            .collect(),
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
pub fn spawn_activity_flush(activity: Arc<ActivityTracker>, backend: Arc<BackendClient>) {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(10));
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
