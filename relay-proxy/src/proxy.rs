//! The fast-lane proxy: per-request classification, activity touch, and
//! upstream selection. All bodies stream — nothing is buffered in full.

use std::time::Duration;

use async_trait::async_trait;
use pingora::http::ResponseHeader;
use pingora::prelude::*;
use pingora::proxy::{ProxyHttp, Session};

use crate::activity::ActivityTracker;
use crate::classify::{classify, delete_session_id, RouteClass};

pub struct RelayProxy {
    appium: (String, u16),
    control: (String, u16),
    proxy_timeout: Duration,
    activity: ActivityTracker,
}

#[derive(Default)]
pub struct RequestCtx {
    fast_lane: bool,
    delete_session: Option<String>,
}

impl RelayProxy {
    pub fn new(appium: (String, u16), control: (String, u16), proxy_timeout: Duration) -> Self {
        Self {
            appium,
            control,
            proxy_timeout,
            activity: ActivityTracker::new(),
        }
    }

    async fn respond_json(session: &mut Session, status: u16, body: String) -> Result<()> {
        let mut header = ResponseHeader::build(status, None)?;
        header.insert_header("Content-Type", "application/json")?;
        header.insert_header("Content-Length", body.len().to_string())?;
        session.write_response_header(Box::new(header), false).await?;
        session
            .write_response_body(Some(body.into_bytes().into()), true)
            .await?;
        Ok(())
    }

    /// Admin endpoints must not leak session ids to the LAN. Allow loopback
    /// peers, plus peers whose source ip equals the listener's own ip (the
    /// agent connecting to a non-wildcard bind_host on the same machine —
    /// a remote host cannot legitimately present the listener's ip as its
    /// TCP source address).
    fn admin_allowed(session: &Session) -> bool {
        let client_ip = session.client_addr().and_then(|a| a.as_inet()).map(|i| i.ip());
        let server_ip = session.server_addr().and_then(|a| a.as_inet()).map(|i| i.ip());
        match (client_ip, server_ip) {
            (Some(c), _) if c.is_loopback() => true,
            (Some(c), Some(s)) => c == s,
            _ => false,
        }
    }
}

#[async_trait]
impl ProxyHttp for RelayProxy {
    type CTX = RequestCtx;

    fn new_ctx(&self) -> RequestCtx {
        RequestCtx::default()
    }

    async fn request_filter(&self, session: &mut Session, ctx: &mut RequestCtx) -> Result<bool> {
        let path = session.req_header().uri.path().to_string();
        match classify(&path) {
            RouteClass::Admin => {
                if !Self::admin_allowed(session) {
                    let header = ResponseHeader::build(403, None)?;
                    session.write_response_header(Box::new(header), true).await?;
                    return Ok(true);
                }
                let body = if path.ends_with("/healthz") {
                    self.activity.healthz_json()
                } else {
                    self.activity.snapshot_json()
                };
                Self::respond_json(session, 200, body).await?;
                Ok(true)
            }
            RouteClass::FastLane { session_id } => {
                self.activity.touch(session_id);
                ctx.fast_lane = true;
                Ok(false)
            }
            RouteClass::Control => {
                let method = session.req_header().method.as_str();
                if let Some(id) = delete_session_id(method, &path) {
                    ctx.delete_session = Some(id.to_string());
                }
                Ok(false)
            }
        }
    }

    async fn upstream_peer(&self, _session: &mut Session, ctx: &mut RequestCtx) -> Result<Box<HttpPeer>> {
        let (host, port) = if ctx.fast_lane { &self.appium } else { &self.control };
        let mut peer = Box::new(HttpPeer::new((host.as_str(), *port), false, String::new()));
        peer.options.connection_timeout = Some(self.proxy_timeout);
        peer.options.read_timeout = Some(self.proxy_timeout);
        peer.options.write_timeout = Some(self.proxy_timeout);
        Ok(peer)
    }

    async fn response_filter(
        &self,
        _session: &mut Session,
        upstream_response: &mut ResponseHeader,
        ctx: &mut RequestCtx,
    ) -> Result<()> {
        // Evict activity once the control plane confirms a session is gone
        // (mirrors the status set http_server.py::delete_session releases on).
        if let Some(id) = ctx.delete_session.take() {
            let code = upstream_response.status.as_u16();
            if (200..300).contains(&code) || code == 404 || code == 410 {
                self.activity.evict(&id);
            }
        }
        Ok(())
    }
}
