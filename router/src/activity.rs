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
}
