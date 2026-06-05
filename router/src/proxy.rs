//! The proxy core: per-request classification, route resolution with lazy
//! rebuild, activity touch, and DELETE-driven pruning. Command bodies stream
//! through pingora — nothing is buffered in full.

use std::sync::Arc;
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
        match classify(&method, &path) {
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
        }
        self.routes.get(session_id)
    }

    /// New-session handler. Implemented in Task 6; for now answers a defined
    /// W3C error rather than panicking.
    async fn handle_new_session(
        &self,
        session: &mut Session,
        _ctx: &mut RouterCtx,
    ) -> Result<bool> {
        respond(
            session,
            500,
            w3c::error_body("session not created", "not implemented yet"),
            "application/json",
        )
        .await
    }
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
