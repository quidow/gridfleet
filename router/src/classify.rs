//! Pure path routing — no GridFleet semantics (classifier shape inherited from
//! the retired relay proxy).

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
}
