//! In-memory session_id -> Appium target map. The router's entire state (spec §2).
//!
//! `std::sync::RwLock` is deliberate: guards are held only for non-awaiting map
//! operations (microseconds), so the executor is never blocked across awaits.
//! (`tokio::sync::RwLock` is for guards held across await points, which never
//! happens here.) `.expect("poisoned")` is deliberate too — a poisoned lock
//! means a panic mid-write, and the correct recovery is a process restart by the
//! container supervisor, not limping on with possibly-inconsistent routes.

use std::collections::HashMap;
use std::sync::RwLock;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Upstream {
    pub host: String,
    pub port: u16,
}

impl Upstream {
    /// Parse "http://host:port" (the backend's target format).
    pub fn parse(target: &str) -> Option<Self> {
        let rest = target.strip_prefix("http://")?;
        let (host, port) = rest.trim_end_matches('/').rsplit_once(':')?;
        if host.is_empty() {
            return None;
        }
        Some(Self {
            host: host.to_string(),
            port: port.parse().ok()?,
        })
    }

    pub fn authority(&self) -> String {
        format!("{}:{}", self.host, self.port)
    }
}

#[derive(Default)]
pub struct RouteMap {
    inner: RwLock<HashMap<String, Upstream>>,
}

impl RouteMap {
    pub fn get(&self, session_id: &str) -> Option<Upstream> {
        self.inner
            .read()
            .expect("poisoned")
            .get(session_id)
            .cloned()
    }
    pub fn insert(&self, session_id: &str, upstream: Upstream) {
        self.inner
            .write()
            .expect("poisoned")
            .insert(session_id.to_string(), upstream);
    }
    pub fn remove(&self, session_id: &str) {
        self.inner.write().expect("poisoned").remove(session_id);
    }
    pub fn len(&self) -> usize {
        self.inner.read().expect("poisoned").len()
    }
    pub fn is_empty(&self) -> bool {
        self.inner.read().expect("poisoned").is_empty()
    }
    pub fn replace_all(&self, entries: Vec<(String, Upstream)>) {
        *self.inner.write().expect("poisoned") = entries.into_iter().collect();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_rejects_empty_host() {
        assert!(Upstream::parse("http://:4723").is_none());
    }

    #[test]
    fn route_map_crud() {
        let m = RouteMap::default();
        assert!(m.get("a").is_none());
        m.insert("a", Upstream::parse("http://10.0.0.5:4723").unwrap());
        assert_eq!(m.get("a").unwrap().authority(), "10.0.0.5:4723");
        assert_eq!(m.len(), 1);
        m.remove("a");
        assert!(m.get("a").is_none());
    }

    #[test]
    fn replace_all_rebuilds() {
        let m = RouteMap::default();
        m.insert("stale", Upstream::parse("http://h:1").unwrap());
        m.replace_all(vec![(
            "fresh".into(),
            Upstream::parse("http://h:2").unwrap(),
        )]);
        assert!(m.get("stale").is_none());
        assert!(m.get("fresh").is_some());
    }
}
