//! HTTP client for the backend's internal grid API (contract: Plan A / spec §3-5).

use std::collections::HashSet;

// Budget constants for the backend-facing HTTP path, with their ordering rules
// checked at compile time. BACKEND_* values are mirrored from the backend
// (authority: LONG_POLL_SEC in app/grid/constants.py, CONFIRM_GRACE_SEC in
// app/grid/allocation.py); retune them there first, then here. See the
// timeout-lattice table in docs/reference/architecture.md.
pub(crate) const CLIENT_TIMEOUT_SECS: u64 = 40;
pub(crate) const CONFIRM_TIMEOUT_SECS: u64 = 10;
pub(crate) const CONFIRM_ATTEMPTS: u64 = 3;
pub(crate) const CONFIRM_RETRY_SLEEP_SECS: u64 = 2;
pub(crate) const BACKEND_LONG_POLL_SECS: u64 = 25;
pub(crate) const BACKEND_CONFIRM_GRACE_SECS: u64 = 60;

// Every allocate long-poll must fit inside the shared client timeout.
const _: () = assert!(CLIENT_TIMEOUT_SECS > BACKEND_LONG_POLL_SECS);
// The worst-case confirm retry budget must fit inside the reaper's grace.
const _: () = assert!(
    CONFIRM_ATTEMPTS * (CONFIRM_TIMEOUT_SECS + CONFIRM_RETRY_SLEEP_SECS)
        < BACKEND_CONFIRM_GRACE_SECS
);
// Each confirm attempt must be far below the shared client timeout.
const _: () = assert!(CONFIRM_TIMEOUT_SECS < CLIENT_TIMEOUT_SECS);

#[derive(Debug)]
pub enum AllocateOutcome {
    Allocated {
        allocation_id: String,
        target: String,
        /// Backend's claim window in seconds: how long the unconfirmed
        /// allocation is held before the reaper releases it. Absent/null from
        /// older backends, in which case the create call is bounded by
        /// `proxy_timeout` alone.
        claim_window_sec: Option<u64>,
        /// The allocated GridFleet device id, injected into the new-session
        /// response caps as `gridfleet:deviceId`. `None` when an older
        /// backend does not supply it (best-effort: no injection then).
        device_id: Option<String>,
    },
    Queued {
        ticket: String,
    },
    Invalid {
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

/// Allocate envelope: the raw W3C body plus the resume ticket and the run
/// binding peeled from the /run/{run_id} endpoint (None = free session).
fn allocate_payload(
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

    pub async fn allocate(
        &self,
        raw_body: &[u8],
        ticket: Option<&str>,
        run_id: Option<&str>,
    ) -> reqwest::Result<AllocateOutcome> {
        let body: serde_json::Value = match serde_json::from_slice(raw_body) {
            Ok(v) => v,
            Err(e) => {
                return Ok(AllocateOutcome::Invalid {
                    message: format!("invalid JSON: {e}"),
                })
            }
        };
        let payload = allocate_payload(body, ticket, run_id);
        let resp = self
            .req(reqwest::Method::POST, "/internal/grid/allocate")
            .json(&payload)
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
                Ok(AllocateOutcome::Invalid { message })
            }
            410 => Ok(AllocateOutcome::QueueTimeout),
            status @ (401 | 403 | 404) => {
                // Permanent misconfiguration: bad backend auth (401/403) or the
                // internal grid API not reachable at this base URL (404).
                // Retrying cannot fix it, so surface it as Fatal so the create
                // fails immediately instead of looping until the deadline.
                let message = match status {
                    401 | 403 => "backend authentication failed".to_string(),
                    _ => "backend internal grid API not found".to_string(),
                };
                Ok(AllocateOutcome::Fatal { status, message })
            }
            status @ 400..=499 => {
                // Any other 4xx is a request-level rejection retrying cannot fix
                // (e.g. FastAPI 422 on a malformed allocate envelope). Surface the
                // body's detail to the client immediately instead of spinning the
                // 2s retry loop to the new-session deadline (wave-5 #5). Only 5xx
                // and transport errors stay transient.
                let raw = resp.text().await.unwrap_or_default();
                let detail = reject_detail(&raw);
                Ok(AllocateOutcome::Invalid {
                    message: format!("backend rejected allocate ({status}): {detail}"),
                })
            }
            _ => {
                let v: serde_json::Value = resp.error_for_status()?.json().await?;
                if v["status"] == "allocated" {
                    Ok(AllocateOutcome::Allocated {
                        allocation_id: v["allocation_id"].as_str().unwrap_or_default().to_string(),
                        target: v["target"].as_str().unwrap_or_default().to_string(),
                        claim_window_sec: v["claim_window_sec"].as_u64(),
                        device_id: v["device_id"].as_str().map(str::to_string),
                    })
                } else {
                    Ok(AllocateOutcome::Queued {
                        ticket: v["ticket"].as_str().unwrap_or_default().to_string(),
                    })
                }
            }
        }
    }

    pub async fn confirm(
        &self,
        allocation_id: &str,
        appium_session_id: &str,
        appium_capabilities: Option<&serde_json::Value>,
    ) -> reqwest::Result<()> {
        let mut body = serde_json::json!({"appium_session_id": appium_session_id});
        if let Some(caps) = appium_capabilities {
            body["appium_capabilities"] = caps.clone();
        }
        self.req(
            reqwest::Method::POST,
            &format!("/internal/grid/sessions/{allocation_id}/confirm"),
        )
        // Confirm is a tiny POST; its retry budget is compile-time asserted below
        // the backend's confirm grace (see the consts at the top of this module).
        .timeout(std::time::Duration::from_secs(CONFIRM_TIMEOUT_SECS))
        .json(&body)
        .send()
        .await?
        .error_for_status()?;
        Ok(())
    }

    pub async fn fail(&self, allocation_id: &str, message: &str) -> reqwest::Result<()> {
        self.req(
            reqwest::Method::POST,
            &format!("/internal/grid/sessions/{allocation_id}/fail"),
        )
        .json(&serde_json::json!({"message": message}))
        .send()
        .await?
        .error_for_status()?;
        Ok(())
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
            &format!("/internal/grid/allocate/{ticket}"),
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
    fn allocate_payload_carries_run_binding() {
        let p =
            super::allocate_payload(serde_json::json!({"capabilities": {}}), None, Some("rid-1"));
        assert_eq!(p["run_id"], "rid-1");
        let free = super::allocate_payload(serde_json::json!({}), Some("t-1"), None);
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
