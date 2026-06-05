//! HTTP client for the backend's internal grid API (contract: Plan A / spec §3-5).

use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

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
    },
    Queued {
        ticket: String,
    },
    Invalid {
        message: String,
    },
    QueueTimeout,
}

pub struct BackendClient {
    base: String,
    auth: Option<(String, String)>,
    http: reqwest::Client,
}

impl BackendClient {
    pub fn new(base: &str, auth: Option<(String, String)>) -> Self {
        let http = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(40)) // > backend's 25s long-poll
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
    ) -> reqwest::Result<AllocateOutcome> {
        let body: serde_json::Value = match serde_json::from_slice(raw_body) {
            Ok(v) => v,
            Err(e) => {
                return Ok(AllocateOutcome::Invalid {
                    message: format!("invalid JSON: {e}"),
                })
            }
        };
        let payload = serde_json::json!({"body": body, "ticket": ticket});
        let resp = self
            .req(reqwest::Method::POST, "/internal/grid/allocate")
            .json(&payload)
            .send()
            .await?;
        match resp.status().as_u16() {
            400 => Ok(AllocateOutcome::Invalid {
                message: resp.text().await.unwrap_or_default(),
            }),
            410 => Ok(AllocateOutcome::QueueTimeout),
            _ => {
                let v: serde_json::Value = resp.error_for_status()?.json().await?;
                if v["status"] == "allocated" {
                    Ok(AllocateOutcome::Allocated {
                        allocation_id: v["allocation_id"].as_str().unwrap_or_default().to_string(),
                        target: v["target"].as_str().unwrap_or_default().to_string(),
                        claim_window_sec: v["claim_window_sec"].as_u64(),
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
    ) -> reqwest::Result<()> {
        self.req(
            reqwest::Method::POST,
            &format!("/internal/grid/sessions/{allocation_id}/confirm"),
        )
        .json(&serde_json::json!({"appium_session_id": appium_session_id}))
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

    pub async fn flush_activity(
        &self,
        sessions: HashMap<String, SystemTime>,
    ) -> reqwest::Result<()> {
        if sessions.is_empty() {
            return Ok(());
        }
        let map: serde_json::Map<String, serde_json::Value> = sessions
            .into_iter()
            .map(|(id, ts)| (id, serde_json::Value::String(rfc3339_utc(ts))))
            .collect();
        self.req(reqwest::Method::POST, "/internal/grid/activity")
            .json(&serde_json::json!({"sessions": map}))
            .send()
            .await?
            .error_for_status()?;
        Ok(())
    }
}

/// Format a `SystemTime` as a UTC RFC3339 timestamp `YYYY-MM-DDTHH:MM:SSZ`.
/// Times before the UNIX epoch clamp to the epoch. Civil-date math uses
/// Howard Hinnant's `days_from_civil` inverse (`civil_from_days`).
fn rfc3339_utc(t: SystemTime) -> String {
    let secs = t
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let (hour, minute, second) = (rem / 3600, (rem % 3600) / 60, rem % 60);
    let (year, month, day) = civil_from_days(days);
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}

/// Inverse of `days_from_civil`: convert days since 1970-01-01 to (year, month, day).
/// From Howard Hinnant's "chrono-compatible Low-Level Date Algorithms".
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m, d)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn rfc3339_epoch() {
        assert_eq!(rfc3339_utc(UNIX_EPOCH), "1970-01-01T00:00:00Z");
    }

    #[test]
    fn rfc3339_known_modern() {
        // date -u -r 1717538096 +%Y-%m-%dT%H:%M:%SZ => 2024-06-04T21:54:56Z
        let t = UNIX_EPOCH + Duration::from_secs(1_717_538_096);
        assert_eq!(rfc3339_utc(t), "2024-06-04T21:54:56Z");
    }

    #[test]
    fn rfc3339_before_epoch_clamps() {
        let t = UNIX_EPOCH - Duration::from_secs(10);
        assert_eq!(rfc3339_utc(t), "1970-01-01T00:00:00Z");
    }
}
