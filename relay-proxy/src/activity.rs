//! Per-session last-activity tracking, exposed to the agent via
//! `/__gridfleet/activity` and consumed by the Python relay's idle expiry.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// Sessions idle longer than this are lazily pruned. The Python side expires
/// Grid sessions long before this; the prune only guards the map against
/// entries whose DELETE the sidecar never observed.
const PRUNE_AFTER: Duration = Duration::from_secs(3600);

pub struct ActivityTracker {
    start_token: String,
    prune_after: Duration,
    last_activity: Mutex<HashMap<String, Instant>>,
}

impl ActivityTracker {
    pub fn new() -> Self {
        Self::with_prune_after(PRUNE_AFTER)
    }

    pub fn with_prune_after(prune_after: Duration) -> Self {
        // Unique per process so the agent can detect sidecar restarts
        // (activity history dies with the old process).
        let start_token = format!(
            "{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        );
        Self {
            start_token,
            prune_after,
            last_activity: Mutex::new(HashMap::new()),
        }
    }

    pub fn touch(&self, session_id: &str) {
        self.last_activity
            .lock()
            .unwrap()
            .insert(session_id.to_string(), Instant::now());
    }

    pub fn evict(&self, session_id: &str) {
        self.last_activity.lock().unwrap().remove(session_id);
    }

    /// JSON for `/__gridfleet/activity`; prunes stale entries as a side effect.
    pub fn snapshot_json(&self) -> String {
        let mut map = self.last_activity.lock().unwrap();
        map.retain(|_, at| at.elapsed() < self.prune_after);
        let sessions: serde_json::Map<String, serde_json::Value> = map
            .iter()
            .map(|(id, at)| {
                (
                    id.clone(),
                    serde_json::json!({"idle_sec": at.elapsed().as_secs_f64()}),
                )
            })
            .collect();
        serde_json::json!({"start_token": self.start_token, "sessions": sessions}).to_string()
    }

    /// JSON for `/__gridfleet/healthz`.
    pub fn healthz_json(&self) -> String {
        serde_json::json!({"ok": true, "start_token": self.start_token}).to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn touch_then_snapshot_lists_session_with_small_idle() {
        let tracker = ActivityTracker::new();
        tracker.touch("abc");
        let parsed: serde_json::Value = serde_json::from_str(&tracker.snapshot_json()).unwrap();
        let idle = parsed["sessions"]["abc"]["idle_sec"].as_f64().unwrap();
        assert!(idle < 1.0, "idle_sec should be near zero, got {idle}");
        assert!(parsed["start_token"].as_str().unwrap().len() > 1);
    }

    #[test]
    fn evict_removes_session() {
        let tracker = ActivityTracker::new();
        tracker.touch("abc");
        tracker.evict("abc");
        let parsed: serde_json::Value = serde_json::from_str(&tracker.snapshot_json()).unwrap();
        assert!(parsed["sessions"].as_object().unwrap().is_empty());
    }

    #[test]
    fn snapshot_prunes_entries_older_than_prune_window() {
        let tracker = ActivityTracker::with_prune_after(Duration::from_millis(10));
        tracker.touch("abc");
        std::thread::sleep(Duration::from_millis(30));
        let parsed: serde_json::Value = serde_json::from_str(&tracker.snapshot_json()).unwrap();
        assert!(parsed["sessions"].as_object().unwrap().is_empty());
    }

    #[test]
    fn healthz_reports_ok_and_same_token() {
        let tracker = ActivityTracker::new();
        let health: serde_json::Value = serde_json::from_str(&tracker.healthz_json()).unwrap();
        let snap: serde_json::Value = serde_json::from_str(&tracker.snapshot_json()).unwrap();
        assert_eq!(health["ok"], true);
        assert_eq!(health["start_token"], snap["start_token"]);
    }
}
