//! HTTP client for the backend's internal grid API (contract: Plan A / spec §3-5).

use std::collections::HashSet;

// Budget constants for the backend-facing HTTP path. BACKEND_* values are
// mirrored from the backend (authority: LONG_POLL_SEC in app/grid/constants.py
// and CREATE_TIMEOUT_CAP_SEC in app/grid/session_create.py); retune them there
// first, then here. See the timeout-lattice table in docs/reference/architecture.md.
pub(crate) const CLIENT_TIMEOUT_SECS: u64 = 40;
pub(crate) const BACKEND_LONG_POLL_SECS: u64 = 25;
pub(crate) const BACKEND_CREATE_TIMEOUT_CAP_SECS: u64 = 240;
/// Per-request timeout for create-session: one long-poll slice, the capped
/// backend-side Appium create, and margin for the promotion transaction.
pub const CREATE_CALL_TIMEOUT_SECS: u64 = 280;

const _: () = assert!(
    CREATE_CALL_TIMEOUT_SECS > BACKEND_LONG_POLL_SECS + BACKEND_CREATE_TIMEOUT_CAP_SECS + 10
);

#[derive(Debug)]
pub enum CreateOutcome {
    Created {
        session_id: String,
        target: String,
        device_id: Option<String>,
        appium_status: u16,
        appium_body: serde_json::Value,
    },
    Queued {
        ticket: String,
    },
    Invalid {
        message: String,
    },
    CreateFailed {
        appium_status: u16,
        appium_body: serde_json::Value,
    },
    CreateError {
        message: String,
    },
    QueueTimeout,
    /// The backend rejected the request in a way that retrying cannot fix:
    /// 401/403 (bad `GRIDFLEET_ROUTER_BACKEND_AUTH`) or 404 (the internal grid
    /// API is not mounted / wrong base URL). The create must fail immediately
    /// rather than spin the 2s-sleep retry loop until the new-session deadline.
    Fatal {
        status: u16,
        message: String,
    },
}

/// Create-session envelope: the W3C body plus the resume ticket and run binding.
fn create_payload(
    body: serde_json::Value,
    ticket: Option<&str>,
    run_id: Option<&str>,
) -> serde_json::Value {
    serde_json::json!({"body": body, "ticket": ticket, "run_id": run_id})
}

pub struct BackendClient {
    base: String,
    auth: Option<(String, String)>,
    http: reqwest::Client,
}

impl BackendClient {
    pub fn new(base: &str, auth: Option<(String, String)>) -> Self {
        let http = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(CLIENT_TIMEOUT_SECS)) // > backend's long-poll; see the const asserts above
            .build()
            .expect("client");
        Self {
            base: base.trim_end_matches('/').to_string(),
            auth,
            http,
        }
    }

    fn req(&self, method: reqwest::Method, path: &str) -> reqwest::RequestBuilder {
        let r = self.http.request(method, format!("{}{}", self.base, path));
        match &self.auth {
            Some((u, p)) => r.basic_auth(u, Some(p)),
            None => r,
        }
    }

    pub async fn create_session(
        &self,
        raw_body: &[u8],
        ticket: Option<&str>,
        run_id: Option<&str>,
    ) -> reqwest::Result<CreateOutcome> {
        let body: serde_json::Value = match serde_json::from_slice(raw_body) {
            Ok(v) => v,
            Err(e) => {
                return Ok(CreateOutcome::Invalid {
                    message: format!("invalid JSON: {e}"),
                })
            }
        };
        let payload = create_payload(body, ticket, run_id);
        let resp = self
            .req(reqwest::Method::POST, "/internal/grid/create-session")
            .json(&payload)
            .timeout(std::time::Duration::from_secs(CREATE_CALL_TIMEOUT_SECS))
            .send()
            .await?;
        match resp.status().as_u16() {
            400 => {
                // Parse the JSON `message` field like the success branch; fall back to
                // the raw text when the body is not the expected JSON shape.
                let raw = resp.text().await.unwrap_or_default();
                let message = serde_json::from_str::<serde_json::Value>(&raw)
                    .ok()
                    .and_then(|v| v["message"].as_str().map(str::to_string))
                    .unwrap_or(raw);
                Ok(CreateOutcome::Invalid { message })
            }
            410 => Ok(CreateOutcome::QueueTimeout),
            status @ (401 | 403 | 404) => {
                // Permanent misconfiguration: bad backend auth (401/403) or the
                // internal grid API not reachable at this base URL (404).
                // Retrying cannot fix it, so surface it as Fatal so the create
                // fails immediately instead of looping until the deadline.
                let message = match status {
                    401 | 403 => "backend authentication failed".to_string(),
                    _ => "backend internal grid API not found".to_string(),
                };
                Ok(CreateOutcome::Fatal { status, message })
            }
            status @ 400..=499 => {
                // Any other 4xx is a request-level rejection retrying cannot fix
                // (e.g. FastAPI 422 on a malformed create-session envelope). Surface the
                // body's detail to the client immediately instead of spinning the
                // 2s retry loop to the new-session deadline (wave-5 #5). Only 5xx
                // and transport errors stay transient.
                let raw = resp.text().await.unwrap_or_default();
                let detail = reject_detail(&raw);
                Ok(CreateOutcome::Invalid {
                    message: format!("backend rejected create-session ({status}): {detail}"),
                })
            }
            _ => {
                let v: serde_json::Value = resp.error_for_status()?.json().await?;
                match v["status"].as_str().unwrap_or_default() {
                    "created" => Ok(CreateOutcome::Created {
                        session_id: v["session_id"].as_str().unwrap_or_default().to_string(),
                        target: v["target"].as_str().unwrap_or_default().to_string(),
                        device_id: v["device_id"].as_str().map(str::to_string),
                        appium_status: v["appium_status"].as_u64().unwrap_or(200) as u16,
                        appium_body: v["appium_body"].clone(),
                    }),
                    "create_failed" => Ok(CreateOutcome::CreateFailed {
                        appium_status: v["appium_status"].as_u64().unwrap_or(500) as u16,
                        appium_body: v["appium_body"].clone(),
                    }),
                    "create_error" => Ok(CreateOutcome::CreateError {
                        message: v["message"]
                            .as_str()
                            .unwrap_or("session create failed")
                            .to_string(),
                    }),
                    _ => Ok(CreateOutcome::Queued {
                        ticket: v["ticket"].as_str().unwrap_or_default().to_string(),
                    }),
                }
            }
        }
    }

    pub async fn session_ended(&self, session_id: &str) -> reqwest::Result<()> {
        self.req(reqwest::Method::POST, "/internal/grid/sessions/ended")
            .json(&serde_json::json!({"session_id": session_id}))
            .send()
            .await?
            .error_for_status()?;
        Ok(())
    }

    pub async fn cancel_ticket(&self, ticket: &str) -> reqwest::Result<()> {
        self.req(
            reqwest::Method::DELETE,
            &format!("/internal/grid/tickets/{ticket}"),
        )
        .send()
        .await?
        .error_for_status()?;
        Ok(())
    }

    pub async fn fetch_routes(&self) -> reqwest::Result<Vec<(String, String)>> {
        let v: serde_json::Value = self
            .req(reqwest::Method::GET, "/internal/grid/routes")
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        let routes = v["routes"]
            .as_array()
            .map(|arr| {
                arr.iter()
                    .filter_map(|r| {
                        let sid = r["session_id"].as_str()?;
                        let target = r["target"].as_str()?;
                        Some((sid.to_string(), target.to_string()))
                    })
                    .collect()
            })
            .unwrap_or_default();
        Ok(routes)
    }

    /// Report which sessions saw traffic since the last flush. The backend
    /// stamps a server-side now() per id (it always ignored caller timestamps
    /// — clock skew here must not extend idle reaping), so the payload is just
    /// the id set (wave-5 #12).
    pub async fn flush_activity(&self, sessions: HashSet<String>) -> reqwest::Result<()> {
        if sessions.is_empty() {
            return Ok(());
        }
        self.req(reqwest::Method::POST, "/internal/grid/activity")
            .json(&serde_json::json!({"sessions": sessions}))
            .send()
            .await?
            .error_for_status()?;
        Ok(())
    }
}

/// Best-effort human-readable detail from a backend rejection body: a
/// top-level `message`, then FastAPI-style `detail` (a string, or an array of
/// validation objects whose `msg` fields are joined — re-review B1: never dump
/// serialized JSON into the client-facing message), else the raw body text.
fn reject_detail(raw: &str) -> String {
    let Ok(v) = serde_json::from_str::<serde_json::Value>(raw) else {
        return raw.to_string();
    };
    if let Some(message) = v["message"].as_str() {
        return message.to_string();
    }
    match &v["detail"] {
        serde_json::Value::String(s) => s.clone(),
        serde_json::Value::Array(items) => {
            let msgs: Vec<&str> = items.iter().filter_map(|i| i["msg"].as_str()).collect();
            if msgs.is_empty() {
                v["detail"].to_string()
            } else {
                msgs.join("; ")
            }
        }
        serde_json::Value::Null => raw.to_string(),
        other => other.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::reject_detail;

    #[test]
    fn create_payload_carries_run_binding() {
        let p = super::create_payload(serde_json::json!({"capabilities": {}}), None, Some("rid-1"));
        assert_eq!(p["run_id"], "rid-1");
        let free = super::create_payload(serde_json::json!({}), Some("t-1"), None);
        assert!(free["run_id"].is_null());
        assert_eq!(free["ticket"], "t-1");
    }

    #[test]
    fn reject_detail_prefers_message() {
        assert_eq!(reject_detail(r#"{"message":"no match"}"#), "no match");
    }

    #[test]
    fn reject_detail_joins_fastapi_msgs() {
        let raw = r#"{"detail":[{"type":"t","msg":"Input should be a valid dictionary"},{"msg":"second"}]}"#;
        assert_eq!(
            reject_detail(raw),
            "Input should be a valid dictionary; second"
        );
    }

    #[test]
    fn reject_detail_string_detail() {
        assert_eq!(
            reject_detail(r#"{"detail":"plain reason"}"#),
            "plain reason"
        );
    }

    #[test]
    fn reject_detail_falls_back_to_raw() {
        assert_eq!(reject_detail("plain crash dump"), "plain crash dump");
        assert_eq!(reject_detail(r#"{"other":1}"#), r#"{"other":1}"#);
    }
}
