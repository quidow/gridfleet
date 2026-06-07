//! Pure path routing — the only GridFleet semantic is the run-scoped prefix
//! (classifier shape inherited from the retired relay proxy).

/// Result of peeling the optional run-scoped endpoint prefix off a path.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RunPrefix {
    /// No `/run/...` prefix — a free-session request.
    None,
    /// A well-formed `/run/{uuid}` prefix; the uuid is returned verbatim.
    Run(String),
    /// A `/run/...` prefix whose segment is not a UUID — route as Unknown.
    Invalid,
}

/// Strip the run-scoped endpoint prefix (run-scoped-endpoint spec §1-2).
/// Returns the prefix classification and the remaining path, which is what
/// gets classified and proxied upstream — Appium only ever sees pure W3C
/// paths. Applied uniformly, so `/run/{uuid}/status` serves /status; only
/// NewSession consumes the run id.
pub fn peel_run_prefix(path: &str) -> (RunPrefix, &str) {
    let Some(rest) = path.strip_prefix("/run/") else {
        return (RunPrefix::None, path);
    };
    let (segment, remainder) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    if is_uuid_segment(segment) {
        (RunPrefix::Run(segment.to_string()), remainder)
    } else {
        (RunPrefix::Invalid, path)
    }
}

/// Hyphenated-UUID shape check (8-4-4-4-12 hex). Hand-rolled to keep the
/// router free of a uuid dependency for one format test; the backend
/// re-validates the id against a real run anyway.
fn is_uuid_segment(s: &str) -> bool {
    let b = s.as_bytes();
    if b.len() != 36 {
        return false;
    }
    b.iter().enumerate().all(|(i, c)| match i {
        8 | 13 | 18 | 23 => *c == b'-',
        _ => c.is_ascii_hexdigit(),
    })
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RouteClass {
    NewSession,
    SessionCommand { session_id: String },
    DeleteSession { session_id: String },
    Status,
    Healthz,
    Metrics,
    Unknown,
}

pub fn classify(method: &str, path: &str) -> RouteClass {
    match (method, path) {
        ("POST", "/session") => return RouteClass::NewSession,
        ("GET", "/status") => return RouteClass::Status,
        ("GET", "/healthz") => return RouteClass::Healthz,
        ("GET", "/metrics") => return RouteClass::Metrics,
        _ => {}
    }
    if let Some(rest) = path.strip_prefix("/session/") {
        // Trim a single trailing slash so `DELETE /session/{id}/` classifies as
        // DeleteSession (not SessionCommand) — otherwise the trailing-slash
        // DELETE skips the route prune + session_ended notify and the device
        // stays busy until the backend idle sweep.
        let rest = rest.strip_suffix('/').unwrap_or(rest);
        let session_id = rest.split('/').next().unwrap_or_default();
        if !session_id.is_empty() {
            if method == "DELETE" && rest == session_id {
                return RouteClass::DeleteSession {
                    session_id: session_id.to_string(),
                };
            }
            return RouteClass::SessionCommand {
                session_id: session_id.to_string(),
            };
        }
    }
    RouteClass::Unknown
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classifies() {
        assert_eq!(classify("POST", "/session"), RouteClass::NewSession);
        assert_eq!(
            classify("GET", "/session/abc-123/screenshot"),
            RouteClass::SessionCommand {
                session_id: "abc-123".into()
            }
        );
        assert_eq!(
            classify("DELETE", "/session/abc-123"),
            RouteClass::DeleteSession {
                session_id: "abc-123".into()
            }
        );
        assert_eq!(
            classify("POST", "/session/abc-123/element"),
            RouteClass::SessionCommand {
                session_id: "abc-123".into()
            }
        );
        assert_eq!(classify("GET", "/status"), RouteClass::Status);
        assert_eq!(classify("GET", "/healthz"), RouteClass::Healthz);
        assert_eq!(classify("GET", "/metrics"), RouteClass::Metrics);
        // Trailing-slash DELETE must still classify as DeleteSession (not a
        // SessionCommand that skips prune + session_ended).
        assert_eq!(
            classify("DELETE", "/session/abc-123/"),
            RouteClass::DeleteSession {
                session_id: "abc-123".into()
            }
        );
        // Trailing-slash command still routes as a SessionCommand.
        assert_eq!(
            classify("POST", "/session/abc-123/element/"),
            RouteClass::SessionCommand {
                session_id: "abc-123".into()
            }
        );
        // A trailing-slash GET on the bare session path is a SessionCommand
        // (only DELETE on the bare id ends the session).
        assert_eq!(
            classify("GET", "/session/abc-123/"),
            RouteClass::SessionCommand {
                session_id: "abc-123".into()
            }
        );
        assert_eq!(classify("GET", "/wd/hub/nonsense"), RouteClass::Unknown);
        assert_eq!(classify("GET", "/session"), RouteClass::Unknown); // GET /session is not W3C
        assert_eq!(
            classify("GET", "/session/abc-123"),
            RouteClass::SessionCommand {
                session_id: "abc-123".into()
            }
        );
    }

    #[test]
    fn peels_run_prefix() {
        const RID: &str = "0c8c057f-3ec1-4b9c-9d2e-9f3a86a2c001";
        assert!(matches!(
            peel_run_prefix(&format!("/run/{RID}/session")),
            (RunPrefix::Run(id), "/session") if id == RID
        ));
        // Bare prefix (no trailing path) normalizes to "/".
        assert!(matches!(
            peel_run_prefix(&format!("/run/{RID}")),
            (RunPrefix::Run(_), "/")
        ));
        // No prefix — free session, path untouched.
        assert!(matches!(
            peel_run_prefix("/session"),
            (RunPrefix::None, "/session")
        ));
        // Garbage segment is Invalid and the path is returned untouched.
        assert!(matches!(
            peel_run_prefix("/run/not-a-uuid/session"),
            (RunPrefix::Invalid, _)
        ));
        assert!(matches!(
            peel_run_prefix("/run//session"),
            (RunPrefix::Invalid, _)
        ));
    }

    #[test]
    fn prefixed_session_command_classifies_after_peel() {
        const RID: &str = "0c8c057f-3ec1-4b9c-9d2e-9f3a86a2c001";
        let path = format!("/run/{RID}/session/abc-123/element");
        let (prefix, stripped) = peel_run_prefix(&path);
        assert!(matches!(prefix, RunPrefix::Run(_)));
        assert_eq!(
            classify("POST", stripped),
            RouteClass::SessionCommand {
                session_id: "abc-123".into()
            }
        );
    }
}
