//! The proxy core: per-request classification, route resolution with lazy
//! rebuild, activity touch, and DELETE-driven pruning. Command bodies stream
//! through pingora — nothing is buffered in full.

use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use pingora::http::ResponseHeader;
use pingora::prelude::*;
use pingora::proxy::{ProxyHttp, Session};

use crate::activity::ActivityTracker;
use crate::backend::BackendClient;
use crate::classify::{classify, RouteClass};
use crate::routes::{RouteMap, Upstream};
use crate::w3c;

pub struct RouterCtx {
    pub upstream: Option<Upstream>,
    pub session_id: Option<String>,
    pub is_delete: bool,
    pub started: Instant,
}

pub struct GridRouter {
    pub routes: Arc<RouteMap>,
    pub activity: Arc<ActivityTracker>,
    pub backend: Arc<BackendClient>,
    pub proxy_timeout: Duration,
    pub new_session_timeout: Duration,
}

#[async_trait]
impl ProxyHttp for GridRouter {
    type CTX = RouterCtx;

    fn new_ctx(&self) -> RouterCtx {
        RouterCtx {
            upstream: None,
            session_id: None,
            is_delete: false,
            started: Instant::now(),
        }
    }

    async fn request_filter(&self, session: &mut Session, ctx: &mut RouterCtx) -> Result<bool> {
        let method = session.req_header().method.as_str().to_string();
        let path = session.req_header().uri.path().to_string();
        let class = classify(&method, &path);
        let label = match &class {
            RouteClass::NewSession => "new_session",
            RouteClass::SessionCommand { .. } => "command",
            RouteClass::DeleteSession { .. } => "delete",
            // Plan classes are (new_session|command|delete|local); healthz/status/
            // metrics/unknown are all local-terminated, so map them to "local".
            RouteClass::Healthz
            | RouteClass::Status
            | RouteClass::Metrics
            | RouteClass::Unknown => "local",
        };
        crate::metrics::metrics()
            .commands_total
            .with_label_values(&[label])
            .inc();
        match class {
            RouteClass::Healthz => respond(session, 200, b"ok".to_vec(), "text/plain").await,
            RouteClass::Status => {
                respond(session, 200, w3c::status_body(), "application/json").await
            }
            RouteClass::Metrics => {
                respond(session, 200, crate::metrics::render(), "text/plain").await
            }
            RouteClass::NewSession => self.handle_new_session(session, ctx).await,
            RouteClass::SessionCommand { session_id } => {
                self.route_session(session, ctx, session_id, false).await
            }
            RouteClass::DeleteSession { session_id } => {
                self.route_session(session, ctx, session_id, true).await
            }
            RouteClass::Unknown => {
                respond(
                    session,
                    404,
                    w3c::error_body("unknown command", &format!("{method} {path}")),
                    "application/json",
                )
                .await
            }
        }
    }

    async fn upstream_peer(
        &self,
        _session: &mut Session,
        ctx: &mut RouterCtx,
    ) -> Result<Box<HttpPeer>> {
        let upstream = ctx.upstream.clone().expect("set by request_filter");
        let mut peer = Box::new(HttpPeer::new(upstream.authority(), false, String::new()));
        peer.options.connection_timeout = Some(Duration::from_secs(5));
        peer.options.read_timeout = Some(self.proxy_timeout);
        peer.options.write_timeout = Some(self.proxy_timeout);
        Ok(peer)
    }

    async fn response_filter(
        &self,
        _session: &mut Session,
        upstream_response: &mut ResponseHeader,
        ctx: &mut RouterCtx,
    ) -> Result<()> {
        if ctx.is_delete {
            let status = upstream_response.status.as_u16();
            // Prune on 2xx or 404 (session already gone upstream). Lossiness
            // accepted: response_filter does not fire on upstream connect/transport
            // failures, so a failed DELETE leaves the route entry and skips
            // session_ended. The periodic route rebuild (Task 7) and the backend's
            // session sweep reconcile any leaked entries.
            if (200..300).contains(&status) || status == 404 {
                if let Some(session_id) = ctx.session_id.clone() {
                    self.routes.remove(&session_id);
                    crate::metrics::metrics()
                        .active_routes
                        .set(self.routes.len() as i64);
                    let backend = self.backend.clone();
                    tokio::spawn(async move {
                        if let Err(e) = backend.session_ended(&session_id).await {
                            log::warn!("session_ended notify failed for {session_id}: {e}");
                        }
                    });
                }
            }
        }
        Ok(())
    }

    async fn logging(&self, _session: &mut Session, _e: Option<&Error>, ctx: &mut RouterCtx) {
        crate::metrics::metrics()
            .request_duration
            .observe(ctx.started.elapsed().as_secs_f64());
    }
}

/// Bound the Appium create call so we never wait past the backend's claim
/// window (the reaper would release the allocation under us). When the backend
/// reports a `claim_window_sec` large enough to honor (> 10s), cap the create
/// at `claim_window_sec - 5s`, but never above the configured `proxy_timeout`.
/// A missing or too-small window falls back to `proxy_timeout` unchanged.
fn create_timeout(proxy_timeout: Duration, claim_window_sec: Option<u64>) -> Duration {
    match claim_window_sec {
        Some(w) if w > 10 => proxy_timeout.min(Duration::from_secs(w - 5)),
        _ => proxy_timeout,
    }
}

/// Increment the allocate-outcome counter (allocated|queued|invalid|timeout|error).
fn alloc_outcome(outcome: &str) {
    crate::metrics::metrics()
        .allocate_outcomes
        .with_label_values(&[outcome])
        .inc();
}

impl GridRouter {
    /// Common path for session commands and DELETE: resolve the upstream,
    /// touch activity, and arm the ctx for `upstream_peer`. Returns a 404 W3C
    /// envelope when no route exists even after a one-shot rebuild.
    async fn route_session(
        &self,
        session: &mut Session,
        ctx: &mut RouterCtx,
        session_id: String,
        is_delete: bool,
    ) -> Result<bool> {
        match self.resolve(&session_id).await {
            Some(upstream) => {
                self.activity.touch(&session_id);
                ctx.is_delete = is_delete;
                ctx.session_id = Some(session_id);
                ctx.upstream = Some(upstream);
                Ok(false) // continue to upstream_peer
            }
            None => {
                respond(
                    session,
                    404,
                    w3c::error_body(
                        "invalid session id",
                        &format!("no route for session {session_id}"),
                    ),
                    "application/json",
                )
                .await
            }
        }
    }

    /// Route-map lookup with one-shot rebuild on miss (covers router restart
    /// racing live sessions). The refetch on miss is deliberately un-coalesced:
    /// concurrent misses may each issue a backend fetch; replace_all is atomic
    /// and idempotent so duplicate fetches are safe.
    async fn resolve(&self, session_id: &str) -> Option<Upstream> {
        if let Some(u) = self.routes.get(session_id) {
            return Some(u);
        }
        if let Ok(entries) = self.backend.fetch_routes().await {
            self.routes.replace_all(
                entries
                    .iter()
                    .filter_map(|(s, t)| Some((s.clone(), Upstream::parse(t)?)))
                    .collect(),
            );
            crate::metrics::metrics()
                .active_routes
                .set(self.routes.len() as i64);
        }
        self.routes.get(session_id)
    }

    /// Confirm an allocation, retrying transport failures (no HTTP status —
    /// e.g. the backend is mid-deploy) up to 3 total attempts, 2s apart. An
    /// HTTP error status (e.g. 409 = allocation already reaped) is permanent
    /// and returned immediately without retry.
    async fn confirm_with_retry(
        &self,
        allocation_id: &str,
        session_id: &str,
    ) -> reqwest::Result<()> {
        for attempt in 1..=3 {
            match self.backend.confirm(allocation_id, session_id).await {
                Ok(()) => return Ok(()),
                Err(e) if e.status().is_some() => return Err(e), // HTTP error: permanent
                Err(e) if attempt == 3 => return Err(e),
                Err(e) => {
                    log::warn!(
                        "confirm transport error for {allocation_id} (attempt {attempt}/3): {e}"
                    );
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
            }
        }
        unreachable!("loop returns on the third attempt")
    }

    /// Best-effort DELETE of an Appium session created during a new-session flow
    /// that we are rolling back. Errors are logged and ignored — the agent's
    /// health checks reclaim a stranded session if this fails.
    async fn delete_appium_session(&self, target: &str, session_id: &str) {
        if let Err(e) = appium_client()
            .delete(format!("{target}/session/{session_id}"))
            .timeout(self.proxy_timeout)
            .send()
            .await
        {
            log::warn!("rollback DELETE of session {session_id} on {target} failed: {e}");
        }
    }

    /// New-session handler: buffer the raw body, long-poll the backend's
    /// allocate endpoint (backend owns queueing/fairness/matching), create the
    /// session on the allocated Appium target with the SAME raw bytes, confirm
    /// with the backend, insert the route, and relay Appium's response
    /// byte-identical.
    async fn handle_new_session(
        &self,
        session: &mut Session,
        _ctx: &mut RouterCtx,
    ) -> Result<bool> {
        // 1. Buffer the raw body (new-session bodies are small; cap at 1 MiB).
        let mut raw = Vec::new();
        while let Some(chunk) = session.read_request_body().await? {
            raw.extend_from_slice(&chunk);
            if raw.len() > 1_048_576 {
                return respond(
                    session,
                    413,
                    w3c::error_body("session not created", "request body too large"),
                    "application/json",
                )
                .await;
            }
        }
        // 2. Allocate (long-poll loop; backend owns queueing/fairness).
        // `new_session_timeout` caps this entire allocate phase only. The Appium
        // create call in step 3 is separately bounded by `proxy_timeout`
        // (deliberate — cold-device session creation can take minutes).
        // Note: a client disconnect during this long-poll is NOT detected; if the
        // client departs while we hold a ticket the session may still be created
        // on its behalf and is subsequently reconciled by the backend idle sweep.
        let deadline = Instant::now() + self.new_session_timeout;
        let mut ticket: Option<String> = None;
        let allocation = loop {
            match self.backend.allocate(&raw, ticket.as_deref()).await {
                Ok(crate::backend::AllocateOutcome::Allocated {
                    allocation_id,
                    target,
                    claim_window_sec,
                }) => {
                    alloc_outcome("allocated");
                    break (allocation_id, target, claim_window_sec);
                }
                Ok(crate::backend::AllocateOutcome::Queued { ticket: t }) => {
                    alloc_outcome("queued");
                    ticket = Some(t);
                }
                Ok(crate::backend::AllocateOutcome::Invalid { message }) => {
                    alloc_outcome("invalid");
                    return respond(
                        session,
                        400,
                        w3c::error_body("session not created", &message),
                        "application/json",
                    )
                    .await;
                }
                Ok(crate::backend::AllocateOutcome::QueueTimeout) => {
                    alloc_outcome("timeout");
                    return respond(
                        session,
                        500,
                        w3c::error_body(
                            "session not created",
                            "no matching device became available",
                        ),
                        "application/json",
                    )
                    .await;
                }
                Err(e) => {
                    alloc_outcome("error");
                    log::warn!("allocate call failed, retrying: {e}");
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
            }
            if Instant::now() >= deadline {
                alloc_outcome("timeout");
                if let Some(t) = &ticket {
                    let _ = self.backend.cancel_ticket(t).await;
                }
                return respond(
                    session,
                    500,
                    w3c::error_body("session not created", "timed out waiting for a device"),
                    "application/json",
                )
                .await;
            }
        };
        let (allocation_id, target, claim_window_sec) = allocation;
        // 3. Create the session on Appium with the SAME raw body (byte-identical).
        // The create call is bounded by the claim window (the backend reaps the
        // unconfirmed allocation after it) so we never hand back a session the
        // backend has already released.
        let resp = appium_client()
            .post(format!("{target}/session"))
            .header("Content-Type", "application/json")
            .body(raw.clone())
            .timeout(create_timeout(self.proxy_timeout, claim_window_sec))
            .send()
            .await;
        match resp {
            Ok(r) if r.status().is_success() => {
                let status = r.status().as_u16();
                let body = r.bytes().await.unwrap_or_default().to_vec();
                let session_id = extract_session_id(&body).unwrap_or_default();
                if session_id.is_empty() {
                    let _ = self
                        .backend
                        .fail(&allocation_id, "appium response missing sessionId")
                        .await;
                    return respond(
                        session,
                        500,
                        w3c::error_body(
                            "session not created",
                            "upstream response missing sessionId",
                        ),
                        "application/json",
                    )
                    .await;
                }
                // 4. Confirm with the backend BEFORE inserting the route or
                // handing the client a session. A failed confirm means the
                // backend may consider the allocation dead, so we roll back the
                // Appium session rather than serve a session it does not track.
                if let Err(e) = self.confirm_with_retry(&allocation_id, &session_id).await {
                    log::warn!(
                        "confirm failed for {allocation_id}, rolling back session {session_id}: {e}"
                    );
                    self.delete_appium_session(&target, &session_id).await;
                    return respond(
                        session,
                        500,
                        w3c::error_body(
                            "session not created",
                            "allocation confirm failed; session was rolled back",
                        ),
                        "application/json",
                    )
                    .await;
                }
                if let Some(upstream) = Upstream::parse(&target) {
                    self.routes.insert(&session_id, upstream);
                    crate::metrics::metrics()
                        .active_routes
                        .set(self.routes.len() as i64);
                } else {
                    log::warn!(
                        "confirmed session {session_id} has unparseable target {target:?}; \
                         no local route inserted — route will appear on next rebuild"
                    );
                }
                respond(session, status, body, "application/json").await
            }
            Ok(r) => {
                let status = r.status().as_u16();
                let body = r.bytes().await.unwrap_or_default().to_vec();
                let _ = self
                    .backend
                    .fail(&allocation_id, &format!("appium returned {status}"))
                    .await;
                respond(session, status, body, "application/json").await
            }
            Err(e) => {
                let _ = self
                    .backend
                    .fail(&allocation_id, &format!("appium unreachable: {e}"))
                    .await;
                respond(
                    session,
                    500,
                    w3c::error_body("session not created", &format!("upstream unreachable: {e}")),
                    "application/json",
                )
                .await
            }
        }
    }
}

/// Shared plain reqwest client for creating Appium sessions (no auth, no base).
/// connect_timeout is set here so unboundedness isn't load-bearing on
/// per-call-site `.timeout()`.
fn appium_client() -> &'static reqwest::Client {
    static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .connect_timeout(std::time::Duration::from_secs(5))
            .build()
            .expect("client")
    })
}

/// value.sessionId per W3C; tolerate legacy top-level sessionId.
fn extract_session_id(body: &[u8]) -> Option<String> {
    let v: serde_json::Value = serde_json::from_slice(body).ok()?;
    v["value"]["sessionId"]
        .as_str()
        .or_else(|| v["sessionId"].as_str())
        .map(str::to_string)
}

async fn respond(
    session: &mut Session,
    status: u16,
    body: Vec<u8>,
    content_type: &str,
) -> Result<bool> {
    let mut header = ResponseHeader::build(status, None)?;
    header.insert_header("Content-Type", content_type)?;
    header.insert_header("Content-Length", body.len().to_string())?;
    session
        .write_response_header(Box::new(header), false)
        .await?;
    session.write_response_body(Some(body.into()), true).await?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::{create_timeout, extract_session_id};
    use std::time::Duration;

    #[test]
    fn create_timeout_none_keeps_proxy_timeout() {
        let pt = Duration::from_secs(300);
        assert_eq!(create_timeout(pt, None), pt);
    }

    #[test]
    fn create_timeout_window_minus_five() {
        assert_eq!(
            create_timeout(Duration::from_secs(300), Some(120)),
            Duration::from_secs(115)
        );
    }

    #[test]
    fn create_timeout_too_small_window_falls_back() {
        let pt = Duration::from_secs(300);
        assert_eq!(create_timeout(pt, Some(8)), pt);
    }

    #[test]
    fn create_timeout_min_wins() {
        assert_eq!(
            create_timeout(Duration::from_secs(60), Some(120)),
            Duration::from_secs(60)
        );
    }

    #[test]
    fn extract_session_id_w3c_shape() {
        let body = br#"{"value":{"sessionId":"app-1","capabilities":{}}}"#;
        assert_eq!(extract_session_id(body).as_deref(), Some("app-1"));
    }

    #[test]
    fn extract_session_id_legacy_top_level() {
        let body = br#"{"sessionId":"legacy-9","status":0}"#;
        assert_eq!(extract_session_id(body).as_deref(), Some("legacy-9"));
    }

    #[test]
    fn extract_session_id_garbage_is_none() {
        assert!(extract_session_id(b"not json").is_none());
        assert!(extract_session_id(br#"{"value":{}}"#).is_none());
    }
}
