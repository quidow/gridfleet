//! Per-session activity accumulator, drained by the periodic flusher (spec §5).
//!
//! Just the *set* of touched session ids: the backend stamps a server-side
//! `now()` for every reported id and always ignored caller timestamps (clock
//! skew on this host must not extend or defeat idle reaping), so tracking
//! per-session `SystemTime`s and serializing RFC3339 was pure maintenance
//! burden (wave-5 #12).

use std::collections::HashSet;
use std::sync::Mutex;

#[derive(Default)]
pub struct ActivityTracker {
    inner: Mutex<HashSet<String>>,
}

impl ActivityTracker {
    pub fn touch(&self, session_id: &str) {
        self.inner
            .lock()
            .expect("poisoned")
            .insert(session_id.to_string());
    }
    pub fn drain(&self) -> HashSet<String> {
        std::mem::take(&mut *self.inner.lock().expect("poisoned"))
    }

    /// Re-insert drained ids after a failed flush so they are not lost. A
    /// concurrent `touch` between the drain and this call is a plain set
    /// union — nothing to reconcile.
    pub fn restore(&self, drained: HashSet<String>) {
        self.inner.lock().expect("poisoned").extend(drained);
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
        assert!(drained.contains("s1"));
        assert!(t.drain().is_empty()); // drained means drained
    }

    #[test]
    fn restore_reinserts_drained_entries() {
        let t = ActivityTracker::default();
        t.touch("s1");
        let drained = t.drain();
        assert!(t.drain().is_empty());
        t.restore(drained);
        assert!(t.drain().contains("s1")); // failed flush did not lose the entry
    }

    #[test]
    fn restore_unions_with_mid_flush_touch() {
        let t = ActivityTracker::default();
        t.touch("s1");
        let drained = t.drain();
        t.touch("s2"); // a fresh touch landed mid-flush
        t.restore(drained);
        let after = t.drain();
        assert!(after.contains("s1") && after.contains("s2"));
    }
}
