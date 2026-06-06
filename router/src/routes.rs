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

/// One map entry: the upstream plus the insert generation at which it was last
/// written by `insert`. The generation lets `replace_if_unchanged` (the cache-
/// miss / reconcile rebuild) preserve any route inserted *after* a stale
/// backend snapshot was captured, instead of evicting it on the wholesale swap.
struct Entry {
    upstream: Upstream,
    /// Generation at which this entry was last `insert`ed. A rebuilt entry
    /// (from `replace_if_unchanged`) carries the generation it was overlaid at
    /// so a still-newer concurrent insert keeps winning.
    inserted_gen: u64,
}

#[derive(Default)]
struct Inner {
    map: HashMap<String, Entry>,
    /// Monotonic counter bumped on every `insert`. Reconcile/miss-path rebuilds
    /// capture this *before* the backend fetch and pass it to
    /// `replace_if_unchanged`; entries with `inserted_gen > captured` survive
    /// the swap.
    insert_gen: u64,
}

#[derive(Default)]
pub struct RouteMap {
    inner: RwLock<Inner>,
}

impl RouteMap {
    pub fn get(&self, session_id: &str) -> Option<Upstream> {
        self.inner
            .read()
            .expect("poisoned")
            .map
            .get(session_id)
            .map(|e| e.upstream.clone())
    }
    pub fn insert(&self, session_id: &str, upstream: Upstream) {
        let mut inner = self.inner.write().expect("poisoned");
        inner.insert_gen += 1;
        let gen = inner.insert_gen;
        inner.map.insert(
            session_id.to_string(),
            Entry {
                upstream,
                inserted_gen: gen,
            },
        );
    }
    pub fn remove(&self, session_id: &str) {
        self.inner.write().expect("poisoned").map.remove(session_id);
    }
    pub fn len(&self) -> usize {
        self.inner.read().expect("poisoned").map.len()
    }
    pub fn is_empty(&self) -> bool {
        self.inner.read().expect("poisoned").map.is_empty()
    }

    /// Capture the current insert generation. A rebuild path calls this BEFORE
    /// fetching the backend snapshot, then passes the value to
    /// `replace_if_unchanged` so any route inserted during the fetch is not
    /// clobbered by the (now-stale) snapshot. See C4.
    pub fn insert_generation(&self) -> u64 {
        self.inner.read().expect("poisoned").insert_gen
    }

    /// Rebuild the map from a backend snapshot captured at generation
    /// `captured_gen`. Routes inserted since the capture (`inserted_gen >
    /// captured_gen`) are overlaid on top of the snapshot so a freshly-confirmed
    /// session that raced the fetch is preserved rather than evicted. All other
    /// entries are replaced wholesale (the reconcile's purpose: drop stale
    /// routes). The generation/lock are read and the swap applied atomically.
    pub fn replace_if_unchanged(&self, entries: Vec<(String, Upstream)>, captured_gen: u64) {
        let mut inner = self.inner.write().expect("poisoned");
        let mut next: HashMap<String, Entry> = entries
            .into_iter()
            .map(|(s, u)| {
                (
                    s,
                    Entry {
                        upstream: u,
                        // Snapshot entries predate the capture; stamp them at the
                        // captured generation so a concurrent insert still wins.
                        inserted_gen: captured_gen,
                    },
                )
            })
            .collect();
        // Overlay every entry inserted after the snapshot was captured.
        for (sid, entry) in inner.map.iter() {
            if entry.inserted_gen > captured_gen {
                next.insert(
                    sid.clone(),
                    Entry {
                        upstream: entry.upstream.clone(),
                        inserted_gen: entry.inserted_gen,
                    },
                );
            }
        }
        inner.map = next;
    }

    #[cfg(test)]
    pub fn replace_all(&self, entries: Vec<(String, Upstream)>) {
        // Test-only: unconditional swap (generation 0 baseline). Production
        // rebuild paths use `replace_if_unchanged` to survive the C4 race.
        self.replace_if_unchanged(entries, self.insert_generation());
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

    #[test]
    fn replace_if_unchanged_preserves_route_inserted_after_capture() {
        // Reproduce the C4 interleaving on a single thread: request B captures
        // the generation, then request A confirms+inserts its fresh route, then
        // B's (now-stale) snapshot — which lacks A's route — lands via
        // replace_if_unchanged. A's route must survive.
        let m = RouteMap::default();
        let captured = m.insert_generation(); // B captures before fetching

        // A confirms and inserts its route mid-fetch.
        m.insert("A-fresh", Upstream::parse("http://h:9").unwrap());

        // B's stale snapshot (running-only, captured before A confirmed) does
        // NOT include A-fresh; it carries some other running session.
        m.replace_if_unchanged(
            vec![("other".into(), Upstream::parse("http://h:1").unwrap())],
            captured,
        );

        // A's freshly-inserted route is preserved, not evicted.
        assert_eq!(m.get("A-fresh").unwrap().authority(), "h:9");
        // The snapshot's own entries still land.
        assert_eq!(m.get("other").unwrap().authority(), "h:1");
    }

    #[test]
    fn replace_if_unchanged_drops_stale_when_no_concurrent_insert() {
        // No insert between capture and replace: the swap is wholesale and
        // stale routes absent from the snapshot are dropped (the reconcile's
        // purpose).
        let m = RouteMap::default();
        m.insert("stale", Upstream::parse("http://h:1").unwrap());
        let captured = m.insert_generation(); // captured AFTER the insert

        m.replace_if_unchanged(
            vec![("fresh".into(), Upstream::parse("http://h:2").unwrap())],
            captured,
        );

        assert!(m.get("stale").is_none(), "stale route must be dropped");
        assert!(m.get("fresh").is_some());
    }

    #[test]
    fn replace_if_unchanged_overlaid_route_survives_second_rebuild() {
        // An overlaid (post-capture) route must keep winning across a later
        // rebuild whose capture also predates it — i.e. inserted_gen tracks the
        // original insert, not the overlay.
        let m = RouteMap::default();
        let gen_a = m.insert_generation();
        m.insert("X", Upstream::parse("http://h:9").unwrap());
        // First rebuild captured before X: X is overlaid, stamped at its own gen.
        m.replace_if_unchanged(vec![], gen_a);
        assert!(m.get("X").is_some());
        // A second rebuild whose capture still predates X must also keep it.
        m.replace_if_unchanged(vec![], gen_a);
        assert_eq!(m.get("X").unwrap().authority(), "h:9");
    }
}
