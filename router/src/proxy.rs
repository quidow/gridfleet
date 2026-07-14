//! The proxy core: per-request classification, route resolution with lazy
//! rebuild, activity touch, and DELETE-driven pruning. Command bodies stream
//! through pingora — nothing is buffered in full.

use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use pingora::http::ResponseHeader;
use pingora::prelude::*;
use pingora::protocols::http::v1::common::is_upgrade_req;
use pingora::proxy::{ProxyHttp, Session};

use crate::activity::ActivityTracker;
use crate::backend::{BackendClient, CreateOutcome};
use crate::classify::{classify, peel_run_prefix, RouteClass, RunPrefix};
use crate::routes::{RouteMap, Upstream};
use crate::w3c;

pub struct RouterCtx {
    pub upstream: Option<Upstream>,
    pub session_id: Option<String>,
    pub is_delete: bool,
    pub started: Instant,
    /// Prefix-stripped URI to forward upstream when the request arrived on the
    /// run-scoped endpoint (commands carry the client's base URL prefix).
    pub stripped_uri: Option<http::Uri>,
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
            stripped_uri: None,
        }
    }

    async fn request_filter(&self, session: &mut Session, ctx: &mut RouterCtx) -> Result<bool> {
        // Peel the optional run-scoped prefix first (spec §2): the run id feeds
        // NewSession allocation; for everything else the prefix is just stripped
        // so Appium sees pure W3C paths. An invalid segment routes as Unknown.
        let (run_id, class) = {
            let req = session.req_header();
            let path = req.uri.path();
            let (prefix, stripped) = peel_run_prefix(path);
            match prefix {
                RunPrefix::Invalid => (None, RouteClass::Unknown),
                RunPrefix::None => (None, classify(req.method.as_str(), path)),
                RunPrefix::Run(id) => {
                    let class = classify(req.method.as_str(), stripped);
                    if matches!(
                        class,
                        RouteClass::SessionCommand { .. } | RouteClass::DeleteSession { .. }
                    ) {
                        ctx.stripped_uri = Some(rebuild_uri(stripped, req.uri.query()));
                    }
                    (Some(id), class)
                }
            }
        };
        let m = crate::metrics::metrics();
        match &class {
            RouteClass::NewSession => m.commands_new_session.inc(),
            RouteClass::SessionCommand { .. } => m.commands_command.inc(),
            RouteClass::DeleteSession { .. } => m.commands_delete.inc(),
            // Plan classes are (new_session|command|delete|local); healthz/status/
            // metrics/unknown are all local-terminated, so map them to "local".
            RouteClass::Healthz
            | RouteClass::Status
            | RouteClass::Metrics
            | RouteClass::Unknown => m.commands_local.inc(),
        }
        match class {
            RouteClass::Healthz => respond(session, 200, b"ok".to_vec(), "text/plain").await,
            RouteClass::Status => {
                respond(session, 200, w3c::status_body(), "application/json").await
            }
            RouteClass::Metrics => {
                respond(session, 200, crate::metrics::render(), "text/plain").await
            }
            RouteClass::NewSession => self.handle_new_session(session, ctx, run_id).await,
            RouteClass::SessionCommand { session_id } => {
                self.route_session(session, ctx, session_id, false).await
            }
            RouteClass::DeleteSession { session_id } => {
                self.route_session(session, ctx, session_id, true).await
            }
            RouteClass::Unknown => {
                let detail = {
                    let req = session.req_header();
                    format!("{} {}", req.method.as_str(), req.uri.path())
                };
                respond(
                    session,
                    404,
                    w3c::error_body("unknown command", &detail),
                    "application/json",
                )
                .await
            }
        }
    }

    async fn upstream_peer(
        &self,
        session: &mut Session,
        ctx: &mut RouterCtx,
    ) -> Result<Box<HttpPeer>> {
        let upstream = ctx.upstream.clone().expect("set by request_filter");
        let mut peer = Box::new(HttpPeer::new(upstream.authority(), false, String::new()));
        peer.options.connection_timeout = Some(Duration::from_secs(5));
        // An Upgrade request (WebSocket — W3C BiDi / CDP on the session path)
        // becomes a long-lived duplex tunnel after the 101: frames can
        // legitimately be minutes apart (paused debugger, slow test), so the
        // per-command read/write timeouts must not tear it down (wave-5 #2).
        // pingora tunnels the upgrade itself; same predicate proxy_h1 uses.
        // Accepted risk: a client sending a bogus Upgrade header opts its own
        // request out of the read timeout. :4444 sits inside the lab network
        // boundary (docs/guides/security.md), so the only victim is itself.
        if !is_upgrade_req(session.req_header()) {
            peer.options.read_timeout = Some(self.proxy_timeout);
            peer.options.write_timeout = Some(self.proxy_timeout);
        }
        Ok(peer)
    }

    async fn upstream_request_filter(
        &self,
        _session: &mut Session,
        upstream_request: &mut pingora::http::RequestHeader,
        ctx: &mut RouterCtx,
    ) -> Result<()> {
        // Run-scoped requests carry the /run/{uuid} prefix on every command;
        // forward the stripped W3C path upstream.
        if let Some(uri) = ctx.stripped_uri.take() {
            upstream_request.set_uri(uri);
        }
        Ok(())
    }

    async fn response_filter(
        &self,
        _session: &mut Session,
        _upstream_response: &mut ResponseHeader,
        ctx: &mut RouterCtx,
    ) -> Result<()> {
        if ctx.is_delete {
            // A client DELETE is unambiguous intent: prune and notify session_ended
            // on ANY upstream response — including a 5xx from a driver hiccup
            // mid-teardown, which previously left the route entry and the backend
            // `running` row pinning the device until the idle timeout (wave-5 #4).
            // If the Appium session actually survived the hiccup, the orphan sweep
            // kills it next tick (the closed row's id is a doomed id). Transport
            // failures never reach this filter; the logging() hook covers those.
            if let Some(session_id) = ctx.session_id.clone() {
                self.prune_and_notify_ended(session_id);
            }
        }
        Ok(())
    }

    async fn logging(&self, _session: &mut Session, e: Option<&Error>, ctx: &mut RouterCtx) {
        crate::metrics::metrics()
            .request_duration
            .observe(ctx.started.elapsed().as_secs_f64());
        // A DELETE that errored before any response means response_filter never
        // ran. The client's DELETE intent is unambiguous, so we still prune the
        // route and notify the backend that the session ended — otherwise the
        // route lingers and the device stays busy until the backend idle sweep.
        if ctx.is_delete && e.is_some() {
            crate::metrics::metrics().delete_orphaned_total.inc();
            if let Some(session_id) = ctx.session_id.clone() {
                self.prune_and_notify_ended(session_id);
            }
        }
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
    /// Remove the local route for a session and fire-and-forget a
    /// `session_ended` notify to the backend. Shared by the DELETE success path
    /// (`response_filter`) and the DELETE transport-failure path (`logging`):
    /// in both cases the client asked for the session to be gone.
    fn prune_and_notify_ended(&self, session_id: String) {
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
    /// concurrent misses may each issue a backend fetch. We capture the route
    /// map's insert generation BEFORE the fetch and rebuild via
    /// `replace_if_unchanged`, so a route inserted by another request
    /// mid-fetch is overlaid on the (now-stale) snapshot instead of being
    /// evicted by the wholesale swap (C4).
    async fn resolve(&self, session_id: &str) -> Option<Upstream> {
        if let Some(u) = self.routes.get(session_id) {
            return Some(u);
        }
        let gen = self.routes.insert_generation();
        if let Ok(entries) = self.backend.fetch_routes().await {
            self.routes.replace_if_unchanged(
                entries
                    .iter()
                    .filter_map(|(s, t)| Some((s.clone(), Upstream::parse(t)?)))
                    .collect(),
                gen,
            );
            crate::metrics::metrics()
                .active_routes
                .set(self.routes.len() as i64);
        }
        self.routes.get(session_id)
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

    /// New-session handler (WS-14.1): buffer the body, ask the backend to
    /// claim/create/record the session, insert the route, and relay Appium's
    /// response. The router no longer touches Appium during creation.
    async fn handle_new_session(
        &self,
        session: &mut Session,
        _ctx: &mut RouterCtx,
        run_id: Option<String>,
    ) -> Result<bool> {
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

        let deadline = Instant::now() + self.new_session_timeout;
        let mut ticket: Option<String> = None;
        let created = loop {
            match self
                .backend
                .create_session(
                    &raw,
                    ticket.as_deref(),
                    run_id.as_deref(),
                    deadline.saturating_duration_since(Instant::now()),
                )
                .await
            {
                Ok(CreateOutcome::Created {
                    session_id,
                    target,
                    device_id,
                    appium_status,
                    appium_body,
                }) => {
                    alloc_outcome("created");
                    break (session_id, target, device_id, appium_status, appium_body);
                }
                Ok(CreateOutcome::Queued { ticket: t }) => {
                    alloc_outcome("queued");
                    ticket = Some(t);
                }
                Ok(CreateOutcome::Invalid { message }) => {
                    alloc_outcome("invalid");
                    return respond(
                        session,
                        400,
                        w3c::error_body("session not created", &message),
                        "application/json",
                    )
                    .await;
                }
                Ok(CreateOutcome::CreateFailed {
                    appium_status,
                    appium_body,
                }) => {
                    alloc_outcome("create_failed");
                    let body = serde_json::to_vec(&appium_body).unwrap_or_default();
                    return respond(session, appium_status, body, "application/json").await;
                }
                Ok(CreateOutcome::CreateError { message }) => {
                    alloc_outcome("create_error");
                    return respond(
                        session,
                        500,
                        w3c::error_body("session not created", &message),
                        "application/json",
                    )
                    .await;
                }
                Ok(CreateOutcome::QueueTimeout) => {
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
                Ok(CreateOutcome::Fatal { status, message }) => {
                    alloc_outcome("fatal");
                    log::error!("create-session failed permanently ({status}): {message}");
                    if let Some(t) = &ticket {
                        let _ = self.backend.cancel_ticket(t).await;
                    }
                    return respond(
                        session,
                        500,
                        w3c::error_body(
                            "session not created",
                            &format!("router misconfigured: {message}"),
                        ),
                        "application/json",
                    )
                    .await;
                }
                Err(e) => {
                    alloc_outcome("error");
                    log::warn!("create-session call failed, retrying: {e}");
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

        let (session_id, target, device_id, appium_status, appium_body) = created;
        let Some(upstream) = Upstream::parse(&target) else {
            log::warn!("created session {session_id} has unparseable target {target:?}");
            self.teardown_lost_session(&target, &session_id, false)
                .await;
            return respond(
                session,
                500,
                w3c::error_body(
                    "session not created",
                    "allocated target was unroutable; session was rolled back",
                ),
                "application/json",
            )
            .await;
        };

        self.routes.insert(&session_id, upstream);
        crate::metrics::metrics()
            .active_routes
            .set(self.routes.len() as i64);
        let body = serde_json::to_vec(&appium_body).unwrap_or_default();
        let relay_body = match &device_id {
            Some(id) => w3c::inject_device_id(&body, id),
            None => body,
        };
        match respond(session, appium_status, relay_body, "application/json").await {
            Ok(v) => Ok(v),
            Err(e) => {
                crate::metrics::metrics()
                    .new_session_client_gone_total
                    .inc();
                log::warn!(
                    "client gone before new-session response for {session_id}; rolling back: {e}"
                );
                self.teardown_lost_session(&target, &session_id, true).await;
                Err(e)
            }
        }
    }

    /// Tear down a created session the client cannot use (an unroutable target,
    /// or the client vanished before the response write).
    async fn teardown_lost_session(&self, target: &str, session_id: &str, prune_route: bool) {
        if prune_route {
            self.routes.remove(session_id);
            crate::metrics::metrics()
                .active_routes
                .set(self.routes.len() as i64);
        }
        self.delete_appium_session(target, session_id).await;
        if let Err(e) = self.backend.session_ended(session_id).await {
            log::warn!("session_ended notify failed for {session_id}: {e}");
        }
    }
}

/// Rebuild the upstream URI from the prefix-stripped path, preserving the query.
fn rebuild_uri(path: &str, query: Option<&str>) -> http::Uri {
    let pq = match query {
        Some(q) => format!("{path}?{q}"),
        None => path.to_string(),
    };
    pq.parse().expect("stripped path is a valid URI path")
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
    #[test]
    fn rebuild_uri_preserves_query() {
        assert_eq!(
            super::rebuild_uri("/session/abc", None).to_string(),
            "/session/abc"
        );
        assert_eq!(
            super::rebuild_uri("/session/abc", Some("a=1&b=2")).to_string(),
            "/session/abc?a=1&b=2"
        );
    }
}
