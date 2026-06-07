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
use crate::backend::BackendClient;
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

/// How to tear down the leaked Appium session during a new-session rollback.
enum AppiumRollback<'a> {
    /// Create/confirm returned a session id: DELETE it directly.
    ById { session_id: &'a str },
    /// Create returned a 2xx with no usable session id: sweep the device-pinned
    /// target's session list and DELETE each.
    SweepTarget,
}

/// What to tell the backend during a new-session rollback.
enum BackendRollback<'a> {
    /// Pre-confirm: the allocation never became a running session — fail it.
    Fail {
        allocation_id: &'a str,
        message: &'a str,
    },
    /// Post-confirm: the backend row is `running` — notify that it ended.
    Ended { session_id: &'a str },
    /// The confirm call itself failed (e.g. 409 already-confirmed/reaped); the
    /// backend already owns the allocation's fate, so notify nothing.
    None,
}

/// One rollback descriptor per `handle_new_session` error branch. Every field
/// is mandatory, so a future branch cannot compile without deciding each step
/// (Q4 — the four hand-rolled subsets are now one path).
struct Rollback<'a> {
    appium: AppiumRollback<'a>,
    backend: BackendRollback<'a>,
    /// Whether a local route was already inserted for this session and must be
    /// pruned. Only meaningful with `AppiumRollback::ById`.
    prune_route: bool,
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

/// Bound the Appium create call so we never wait past the backend's claim
/// window (the reaper would release the allocation under us). For any reported
/// `claim_window_sec`, cap the create at `max(window - 5, 5)s` and never above
/// the configured `proxy_timeout`. The 5s floor keeps a tiny window from
/// yielding a zero/negative budget. A missing window falls back to
/// `proxy_timeout` unchanged.
fn create_timeout(proxy_timeout: Duration, claim_window_sec: Option<u64>) -> Duration {
    match claim_window_sec {
        Some(w) => proxy_timeout.min(Duration::from_secs(w.saturating_sub(5).max(5))),
        None => proxy_timeout,
    }
}

/// A confirm error is permanent only when the backend returned a 4xx that is
/// not 429: 409 (allocation already reaped/confirmed) is the designed case,
/// 400/422 mean a malformed/rejected request we cannot fix by retrying.
/// Transport errors (no status), 5xx, and 429 are transient and worth a retry.
fn is_permanent_confirm_error(e: &reqwest::Error) -> bool {
    match e.status() {
        Some(s) => s.is_client_error() && s != reqwest::StatusCode::TOO_MANY_REQUESTS,
        None => false,
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
    /// `replace_if_unchanged`, so a route confirmed+inserted by another request
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

    /// Confirm an allocation, retrying up to 3 total attempts, 2s apart.
    /// Transport failures (no HTTP status — e.g. the backend is mid-deploy) and
    /// retryable HTTP statuses (5xx, 429 — transient backend pressure or a
    /// rolling deploy) are retried. Only a 4xx other than 429 is permanent
    /// (409 = allocation already reaped/confirmed is the designed case) and is
    /// returned immediately without retry; the per-request 10s timeout bounds
    /// each attempt.
    async fn confirm_with_retry(
        &self,
        allocation_id: &str,
        session_id: &str,
        appium_capabilities: Option<&serde_json::Value>,
    ) -> reqwest::Result<()> {
        for attempt in 1..=3 {
            match self
                .backend
                .confirm(allocation_id, session_id, appium_capabilities)
                .await
            {
                Ok(()) => return Ok(()),
                Err(e) if is_permanent_confirm_error(&e) => return Err(e),
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

    /// Best-effort cleanup when Appium returns a 2xx create whose body has no
    /// sessionId: we cannot DELETE by id (we have none), so enumerate the
    /// target's sessions and DELETE each. The node is device-pinned and was
    /// just allocated to us, so any live session on it is the one we just
    /// created (or a stale one we also want gone). The list endpoint
    /// (`GET /appium/sessions`) may 4xx when Appium's insecure feature flag is
    /// off — that's tolerated; we simply skip the sweep and rely on the agent's
    /// health checks / backend idle sweep to reclaim the stranded session.
    async fn sweep_target_sessions(&self, target: &str) {
        let resp = appium_client()
            .get(format!("{target}/appium/sessions"))
            .timeout(self.proxy_timeout)
            .send()
            .await;
        let body = match resp {
            Ok(r) if r.status().is_success() => r.bytes().await.unwrap_or_default(),
            Ok(r) => {
                log::warn!(
                    "session sweep: list on {target} returned {}; skipping (insecure feature likely off)",
                    r.status()
                );
                return;
            }
            Err(e) => {
                log::warn!("session sweep: list on {target} failed: {e}");
                return;
            }
        };
        for id in w3c::extract_session_ids(&body) {
            self.delete_appium_session(target, &id).await;
        }
    }

    /// Single rollback path for a half-created new-session flow. Every error
    /// branch in `handle_new_session` builds one `Rollback` and calls this, so
    /// the full set of cleanup steps (Appium teardown, backend notify, local
    /// route prune) lives in one place. Adding a new error branch forces the
    /// author to fill every `Rollback` field — none can be silently forgotten
    /// (Q4). Order: prune the local route first (stop routing new commands to a
    /// session we are tearing down), then DELETE on Appium, then notify the
    /// backend.
    async fn rollback_created_session(&self, target: &str, plan: Rollback<'_>) {
        if plan.prune_route {
            if let AppiumRollback::ById { session_id } = plan.appium {
                self.routes.remove(session_id);
                crate::metrics::metrics()
                    .active_routes
                    .set(self.routes.len() as i64);
            }
        }
        match plan.appium {
            AppiumRollback::ById { session_id } => {
                self.delete_appium_session(target, session_id).await
            }
            AppiumRollback::SweepTarget => self.sweep_target_sessions(target).await,
        }
        match plan.backend {
            BackendRollback::Fail {
                allocation_id,
                message,
            } => {
                let _ = self.backend.fail(allocation_id, message).await;
            }
            BackendRollback::Ended { session_id } => {
                if let Err(e) = self.backend.session_ended(session_id).await {
                    log::warn!("session_ended notify failed for {session_id}: {e}");
                }
            }
            BackendRollback::None => {}
        }
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
        run_id: Option<String>,
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
        // Note: a client disconnect during this long-poll is NOT detected
        // mid-loop. pingora 0.8's `Session` exposes no cheap, non-destructive
        // downstream-liveness probe — `stream()` yields a `Box<dyn IO>` whose
        // trait offers only `shutdown`/`id`/digest accessors, and the body is
        // already fully drained above, so the only way to observe a half-closed
        // client is a blocking async read that would consume bytes. We therefore
        // do NOT break the loop early; instead the success-path `respond` write
        // is the terminal guard: when the client is gone its write fails and we
        // roll the created+confirmed session back (DELETE + session_ended +
        // route prune) rather than leaking a zombie that pins the device.
        let deadline = Instant::now() + self.new_session_timeout;
        let mut ticket: Option<String> = None;
        let allocation = loop {
            match self
                .backend
                .allocate(&raw, ticket.as_deref(), run_id.as_deref())
                .await
            {
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
                Ok(crate::backend::AllocateOutcome::Fatal { status, message }) => {
                    // Permanent backend misconfiguration (bad auth / API not
                    // mounted). Fail the create immediately rather than spinning
                    // the 2s-sleep retry loop until the new-session deadline.
                    alloc_outcome("fatal");
                    log::error!("allocate failed permanently ({status}): {message}");
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
            .body(raw)
            .timeout(create_timeout(self.proxy_timeout, claim_window_sec))
            .send()
            .await;
        match resp {
            Ok(r) if r.status().is_success() => {
                let status = r.status().as_u16();
                let body = r.bytes().await.unwrap_or_default().to_vec();
                let session_id = w3c::extract_session_id(&body).unwrap_or_default();
                let actual_caps = w3c::extract_session_capabilities(&body);
                if session_id.is_empty() {
                    crate::metrics::metrics()
                        .create_missing_session_id_total
                        .inc();
                    // The session may have been created on the node despite the
                    // odd response shape; we have no id to DELETE, so sweep the
                    // (device-pinned) target. Pre-confirm, so fail the allocation.
                    self.rollback_created_session(
                        &target,
                        Rollback {
                            appium: AppiumRollback::SweepTarget,
                            backend: BackendRollback::Fail {
                                allocation_id: &allocation_id,
                                message: "appium response missing sessionId",
                            },
                            prune_route: false,
                        },
                    )
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
                if let Err(e) = self
                    .confirm_with_retry(&allocation_id, &session_id, actual_caps.as_ref())
                    .await
                {
                    log::warn!(
                        "confirm failed for {allocation_id}, rolling back session {session_id}: {e}"
                    );
                    // Confirm itself failed (may be a 409 already-confirmed/
                    // reaped): the backend owns the allocation, so notify
                    // nothing — just DELETE the Appium session. No route yet.
                    self.rollback_created_session(
                        &target,
                        Rollback {
                            appium: AppiumRollback::ById {
                                session_id: &session_id,
                            },
                            backend: BackendRollback::None,
                            prune_route: false,
                        },
                    )
                    .await;
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
                let Some(upstream) = Upstream::parse(&target) else {
                    // Confirm already succeeded, so the backend row is `running`,
                    // but the target is unroutable — every future command would
                    // 404 locally and the route would never rebuild (rebuild
                    // paths drop unparseable targets too). Roll the session back:
                    // DELETE it on Appium, then tell the backend it ended, and
                    // fail the create rather than hand back a dead session.
                    log::warn!(
                        "confirmed session {session_id} has unparseable target {target:?}; \
                         rolling back (DELETE + session_ended) and failing create"
                    );
                    // Confirm succeeded → backend row is `running`: notify ended.
                    // No route was inserted (parse failed), so nothing to prune.
                    self.rollback_created_session(
                        &target,
                        Rollback {
                            appium: AppiumRollback::ById {
                                session_id: &session_id,
                            },
                            backend: BackendRollback::Ended {
                                session_id: &session_id,
                            },
                            prune_route: false,
                        },
                    )
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
                // Hand the session to the client. The route is inserted and the
                // confirm is already sent, so if this write fails (the client
                // disconnected during the allocate long-poll — see step 2's
                // note) the session would otherwise be a confirmed, routed
                // zombie pinning its device until a manual DELETE or the backend
                // idle sweep. Roll the just-created session back: DELETE it on
                // Appium, tell the backend it ended (the row is `running`
                // post-confirm), and drop the local route. This is the terminal
                // guard for the undetected-disconnect case.
                match respond(session, status, body, "application/json").await {
                    Ok(v) => Ok(v),
                    Err(e) => {
                        crate::metrics::metrics()
                            .new_session_client_gone_total
                            .inc();
                        log::warn!(
                            "client gone before new-session response for {session_id}; \
                             rolling back (DELETE + session_ended + route prune): {e}"
                        );
                        // Confirmed + routed: prune the route, DELETE on Appium,
                        // and notify the backend the (running) session ended.
                        self.rollback_created_session(
                            &target,
                            Rollback {
                                appium: AppiumRollback::ById {
                                    session_id: &session_id,
                                },
                                backend: BackendRollback::Ended {
                                    session_id: &session_id,
                                },
                                prune_route: true,
                            },
                        )
                        .await;
                        Err(e)
                    }
                }
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
    use super::create_timeout;
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
    fn create_timeout_small_window_floors_at_five() {
        // max(8-5, 5) = 5, well under proxy_timeout, so the floor wins.
        let pt = Duration::from_secs(300);
        assert_eq!(create_timeout(pt, Some(8)), Duration::from_secs(5));
    }

    #[test]
    fn create_timeout_tiny_window_floors_at_five() {
        // The registry min is 5; max(5-5, 5) = 5 keeps a sane create budget.
        let pt = Duration::from_secs(300);
        assert_eq!(create_timeout(pt, Some(5)), Duration::from_secs(5));
    }

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

    #[test]
    fn create_timeout_min_wins() {
        assert_eq!(
            create_timeout(Duration::from_secs(60), Some(120)),
            Duration::from_secs(60)
        );
    }
}
