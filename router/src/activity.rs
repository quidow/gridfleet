//! Per-session last-activity accumulator, drained by the periodic flusher (spec §5).

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::SystemTime;

#[derive(Default)]
pub struct ActivityTracker {
    inner: Mutex<HashMap<String, SystemTime>>,
}

impl ActivityTracker {
    pub fn touch(&self, session_id: &str) {
        self.inner
            .lock()
            .expect("poisoned")
            .insert(session_id.to_string(), SystemTime::now());
    }
    pub fn drain(&self) -> HashMap<String, SystemTime> {
        std::mem::take(&mut *self.inner.lock().expect("poisoned"))
    }

    /// Re-insert drained entries after a failed flush so their timestamps are not
    /// lost. A concurrent `touch` between the drain and this call leaves a newer
    /// timestamp in the map; we keep whichever is newer per session, so a fresh
    /// touch is never clobbered by a stale restored value.
    pub fn restore(&self, drained: HashMap<String, SystemTime>) {
        let mut map = self.inner.lock().expect("poisoned");
        for (sid, ts) in drained {
            map.entry(sid)
                .and_modify(|cur| {
                    if ts > *cur {
                        *cur = ts;
                    }
                })
                .or_insert(ts);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tracks_and_drains() {
        let t = ActivityTracker::default();
        t.touch("s1");
        let drained = t.drain();
        assert!(drained.contains_key("s1"));
        assert!(t.drain().is_empty()); // drained means drained
    }

    #[test]
    fn restore_reinserts_drained_entries() {
        let t = ActivityTracker::default();
        t.touch("s1");
        let drained = t.drain();
        assert!(t.drain().is_empty());
        t.restore(drained);
        assert!(t.drain().contains_key("s1")); // failed flush did not lose the entry
    }

    #[test]
    fn restore_keeps_newer_touch() {
        let mut drained = HashMap::new();
        let old = SystemTime::UNIX_EPOCH;
        drained.insert("s1".to_string(), old);

        let t = ActivityTracker::default();
        t.touch("s1"); // a fresh touch landed mid-flush; its timestamp is "now"
        t.restore(drained);

        let after = t.drain();
        // The newer mid-flush touch must win over the stale restored value.
        assert!(after["s1"] > old);
    }
}
