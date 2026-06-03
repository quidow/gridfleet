//! Path classification for the relay fast lane.
//!
//! The sidecar must stay free of GridFleet semantics: everything here is
//! pure string routing. Anything not recognized as a session command or a
//! sidecar admin endpoint goes to the control (Python relay) upstream.

#[derive(Debug, PartialEq, Eq)]
pub enum RouteClass<'a> {
    /// `/session/{id}/...` — WebDriver command, proxied straight to Appium.
    FastLane { session_id: &'a str },
    /// `/__gridfleet/healthz|activity` — served by the sidecar itself.
    Admin,
    /// Everything else — control plane, proxied to the Python relay.
    Control,
}

pub fn classify(path: &str) -> RouteClass<'_> {
    if path == "/__gridfleet/healthz" || path == "/__gridfleet/activity" {
        return RouteClass::Admin;
    }
    let mut parts = path.trim_start_matches('/').splitn(3, '/');
    if let (Some("session"), Some(id), Some(rest)) = (parts.next(), parts.next(), parts.next()) {
        if !id.is_empty() && !rest.is_empty() {
            return RouteClass::FastLane { session_id: id };
        }
    }
    RouteClass::Control
}

/// `DELETE /session/{id}` (the bare session teardown) — the sidecar routes it
/// to control but must evict the activity entry when the response confirms
/// the session is gone.
pub fn delete_session_id<'a>(method: &str, path: &'a str) -> Option<&'a str> {
    if method != "DELETE" {
        return None;
    }
    let mut parts = path.trim_start_matches('/').splitn(3, '/');
    match (parts.next(), parts.next(), parts.next()) {
        (Some("session"), Some(id), None) if !id.is_empty() => Some(id),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn command_below_session_id_is_fast_lane() {
        assert_eq!(
            classify("/session/abc/element"),
            RouteClass::FastLane { session_id: "abc" }
        );
        assert_eq!(
            classify("/session/abc/element/0/click"),
            RouteClass::FastLane { session_id: "abc" }
        );
    }

    #[test]
    fn control_paths_stay_on_control() {
        assert_eq!(classify("/status"), RouteClass::Control);
        assert_eq!(classify("/session"), RouteClass::Control); // POST create
        assert_eq!(classify("/session/abc"), RouteClass::Control); // bare DELETE
        assert_eq!(classify("/session/abc/"), RouteClass::Control); // no command segment
        assert_eq!(classify("/se/grid/node/drain"), RouteClass::Control);
        assert_eq!(classify("/"), RouteClass::Control);
        assert_eq!(classify(""), RouteClass::Control);
    }

    #[test]
    fn admin_endpoints_are_exact_matches() {
        assert_eq!(classify("/__gridfleet/healthz"), RouteClass::Admin);
        assert_eq!(classify("/__gridfleet/activity"), RouteClass::Admin);
        // unknown admin-prefixed paths fall through to control (defense in depth)
        assert_eq!(classify("/__gridfleet/unknown"), RouteClass::Control);
    }

    #[test]
    fn delete_session_id_matches_only_bare_delete() {
        assert_eq!(delete_session_id("DELETE", "/session/abc"), Some("abc"));
        assert_eq!(delete_session_id("GET", "/session/abc"), None);
        assert_eq!(delete_session_id("DELETE", "/session/abc/element"), None);
        assert_eq!(delete_session_id("DELETE", "/session"), None);
        assert_eq!(delete_session_id("DELETE", "/status"), None);
    }
}
