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
    /// Parse "http://host:port" (the backend's target format). Bracketed IPv6
    /// (`http://[::1]:4723`) is handled explicitly; an unbracketed IPv6 literal
    /// (`http://::1:4723`) is rejected — the backend never emits it, and the
    /// `host` field is stored bracket-free so `authority()` re-wraps it.
    pub fn parse(target: &str) -> Option<Self> {
        let rest = target.strip_prefix("http://")?.trim_end_matches('/');
        let (host, port) = if let Some(after_bracket) = rest.strip_prefix('[') {
            // Bracketed IPv6: split on the closing bracket, then expect `:port`.
            let (host, tail) = after_bracket.split_once(']')?;
            let port = tail.strip_prefix(':')?;
            (host, port)
        } else {
            let (host, port) = rest.rsplit_once(':')?;
            // A ':' remaining in the host means an unbracketed IPv6 literal
            // (or other malformed authority) — reject defensively.
            if host.contains(':') {
                return None;
            }
            (host, port)
        };
        if host.is_empty() {
            return None;
        }
        Some(Self {
            host: host.to_string(),
            port: port.parse().ok()?,
        })
    }

    /// Authority for `HttpPeer::new`. IPv6 hosts (detected by an embedded ':')
    /// are re-wrapped in brackets so the authority stays valid.
    pub fn authority(&self) -> String {
        if self.host.contains(':') {
            format!("[{}]:{}", self.host, self.port)
        } else {
            format!("{}:{}", self.host, self.port)
        }
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
    fn parse_ipv4_and_hostname_unchanged() {
        let v4 = Upstream::parse("http://10.0.0.5:4723").unwrap();
        assert_eq!(v4.host, "10.0.0.5");
        assert_eq!(v4.port, 4723);
        assert_eq!(v4.authority(), "10.0.0.5:4723");

        let host = Upstream::parse("http://device-host:4723/").unwrap();
        assert_eq!(host.host, "device-host");
        assert_eq!(host.port, 4723);
        assert_eq!(host.authority(), "device-host:4723");
    }

    #[test]
    fn parse_bracketed_ipv6_ok() {
        let u = Upstream::parse("http://[::1]:4723").unwrap();
        assert_eq!(u.host, "::1");
        assert_eq!(u.port, 4723);
        // authority() re-wraps the IPv6 host in brackets for HttpPeer::new.
        assert_eq!(u.authority(), "[::1]:4723");
    }

    #[test]
    fn parse_bracketed_ipv6_full_address() {
        let u = Upstream::parse("http://[2001:db8::1]:4723/").unwrap();
        assert_eq!(u.host, "2001:db8::1");
        assert_eq!(u.port, 4723);
        assert_eq!(u.authority(), "[2001:db8::1]:4723");
    }

    #[test]
    fn parse_unbracketed_ipv6_rejected() {
        // Defensive: the backend never emits this, but rsplit would mangle it
        // into host="::1", port="4723" — reject instead.
        assert!(Upstream::parse("http://::1:4723").is_none());
    }

    #[test]
    fn parse_bracketed_ipv6_missing_port_rejected() {
        assert!(Upstream::parse("http://[::1]").is_none());
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
