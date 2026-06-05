//! Black-box test: launches the compiled router binary against stub Appium and
//! stub backend upstreams and asserts the command-path routing contract.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::{Child, Command};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

/// Minimal Appium stub: echoes method+path+body as JSON. A `/big`-suffixed
/// path is not needed here; we echo whatever body arrives so large payloads
/// round-trip verbatim inside the echoed JSON.
fn spawn_appium() -> String {
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let addr = format!("http://{}", server.server_addr());
    thread::spawn(move || {
        for mut req in server.incoming_requests() {
            let method = req.method().as_str().to_string();
            let path = req.url().to_string();
            let mut body = String::new();
            req.as_reader().read_to_string(&mut body).unwrap();
            let payload = serde_json::json!({
                "upstream": "appium",
                "method": method,
                "path": path,
                "body": body,
            });
            let resp = tiny_http::Response::from_string(payload.to_string()).with_header(
                tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
                    .unwrap(),
            );
            req.respond(resp).unwrap();
        }
    });
    addr
}

/// Backend stub: serves GET /internal/grid/routes with a single known session
/// and counts how many times the routes endpoint was hit (the router must
/// refresh once on a cache miss before answering 404).
fn spawn_backend(appium_addr: String) -> (String, Arc<AtomicUsize>) {
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let addr = format!("http://{}", server.server_addr());
    let routes_hits = Arc::new(AtomicUsize::new(0));
    let hits = routes_hits.clone();
    thread::spawn(move || {
        for req in server.incoming_requests() {
            let url = req.url().to_string();
            if url == "/internal/grid/routes" {
                hits.fetch_add(1, Ordering::SeqCst);
                let body = serde_json::json!({
                    "routes": [{"session_id": "known-session", "target": appium_addr}],
                })
                .to_string();
                let resp = tiny_http::Response::from_string(body).with_header(
                    tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
                        .unwrap(),
                );
                req.respond(resp).unwrap();
            } else {
                // session_ended and friends: accept everything else.
                req.respond(tiny_http::Response::from_string("{}")).unwrap();
            }
        }
    });
    (addr, routes_hits)
}

struct Router {
    child: Child,
    port: u16,
}

impl Drop for Router {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

fn spawn_router(backend_addr: &str) -> Router {
    let listen_port = free_port();
    let child = Command::new(env!("CARGO_BIN_EXE_gridfleet-router"))
        .args([
            "--listen",
            &format!("127.0.0.1:{listen_port}"),
            "--backend",
            backend_addr,
            "--proxy-timeout",
            "5",
        ])
        .spawn()
        .unwrap();
    let router = Router {
        child,
        port: listen_port,
    };
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Ok(resp) = ureq::get(&format!("http://127.0.0.1:{listen_port}/healthz")).call() {
            if resp.status() == 200 {
                return router;
            }
        }
        assert!(
            Instant::now() < deadline,
            "router did not become healthy within the startup window"
        );
        thread::sleep(Duration::from_millis(50));
    }
}

#[test]
fn proxies_session_commands_via_rebuilt_routes() {
    let appium_addr = spawn_appium();
    let (backend_addr, routes_hits) = spawn_backend(appium_addr);
    let router = spawn_router(&backend_addr);
    let base = format!("http://127.0.0.1:{}", router.port);

    // /status -> 200 ready.
    let body = ureq::get(&format!("{base}/status"))
        .call()
        .unwrap()
        .into_string()
        .unwrap();
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["value"]["ready"], true, "got: {body}");

    // Known session command -> proxied to stub appium (cache miss triggers one
    // rebuild, then the route resolves).
    let body = ureq::post(&format!("{base}/session/known-session/url"))
        .send_string(r#"{"url":"http://example.com"}"#)
        .unwrap()
        .into_string()
        .unwrap();
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["upstream"], "appium", "got: {body}");
    assert_eq!(v["path"], "/session/known-session/url", "got: {body}");
    let hits_after_known = routes_hits.load(Ordering::SeqCst);
    assert!(hits_after_known >= 1, "expected a routes rebuild on miss");

    // Unknown session -> 404 invalid session id, AND a fresh routes refresh.
    let err = ureq::get(&format!("{base}/session/unknown/url")).call();
    match err {
        Err(ureq::Error::Status(404, resp)) => {
            let body = resp.into_string().unwrap();
            let v: serde_json::Value = serde_json::from_str(&body).unwrap();
            assert_eq!(v["value"]["error"], "invalid session id", "got: {body}");
        }
        other => panic!("expected 404, got {other:?}"),
    }
    assert!(
        routes_hits.load(Ordering::SeqCst) > hits_after_known,
        "router must refresh routes once before answering 404"
    );

    // Unknown command -> 404 unknown command.
    let err = ureq::get(&format!("{base}/nonsense")).call();
    match err {
        Err(ureq::Error::Status(404, resp)) => {
            let body = resp.into_string().unwrap();
            let v: serde_json::Value = serde_json::from_str(&body).unwrap();
            assert_eq!(v["value"]["error"], "unknown command", "got: {body}");
        }
        other => panic!("expected 404, got {other:?}"),
    }
}

#[test]
fn serves_metrics() {
    let appium_addr = spawn_appium();
    let (backend_addr, _hits) = spawn_backend(appium_addr);
    let router = spawn_router(&backend_addr);
    let base = format!("http://127.0.0.1:{}", router.port);

    let resp = ureq::get(&format!("{base}/metrics")).call().unwrap();
    assert_eq!(resp.status(), 200);
    let body = resp.into_string().unwrap();
    assert!(
        body.contains("gridfleet_router"),
        "metrics body missing prefix: {body}"
    );
}

#[test]
fn streams_large_bodies() {
    let appium_addr = spawn_appium();
    let (backend_addr, _hits) = spawn_backend(appium_addr);
    let router = spawn_router(&backend_addr);
    let base = format!("http://127.0.0.1:{}", router.port);

    let blob = "x".repeat(2_000_000);
    let payload = format!("{{\"data\":\"{blob}\"}}");
    let resp = ureq::post(&format!("{base}/session/known-session/value"))
        .send_string(&payload)
        .unwrap()
        .into_string()
        .unwrap();
    let v: serde_json::Value = serde_json::from_str(&resp).unwrap();
    let echoed = v["body"].as_str().unwrap();
    assert_eq!(echoed.len(), payload.len(), "large body truncated");
}

/// Appium stub for new-session flows: POST /session returns the supplied
/// `(status, body)`; every other path echoes method+path+body as JSON (so
/// command proxying after session creation round-trips verbatim).
fn spawn_appium_new_session(status: u16, session_body: &'static str) -> String {
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let addr = format!("http://{}", server.server_addr());
    thread::spawn(move || {
        for mut req in server.incoming_requests() {
            let method = req.method().as_str().to_string();
            let path = req.url().to_string();
            let mut body = String::new();
            req.as_reader().read_to_string(&mut body).unwrap();
            let json_header =
                tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
                    .unwrap();
            if method == "POST" && path == "/session" {
                let resp = tiny_http::Response::from_string(session_body)
                    .with_status_code(status)
                    .with_header(json_header);
                req.respond(resp).unwrap();
            } else {
                let payload = serde_json::json!({
                    "upstream": "appium",
                    "method": method,
                    "path": path,
                    "body": body,
                });
                let resp =
                    tiny_http::Response::from_string(payload.to_string()).with_header(json_header);
                req.respond(resp).unwrap();
            }
        }
    });
    addr
}

/// Counts of the new-session backend endpoints the router calls.
#[derive(Clone)]
struct AllocCounts {
    confirms: Arc<AtomicUsize>,
    fails: Arc<AtomicUsize>,
    routes: Arc<AtomicUsize>,
}

/// Backend stub for new-session flows. `allocate_status` is the HTTP status
/// for POST /internal/grid/allocate; `allocate_body` its JSON body (ignored on
/// non-2xx). When `queue_first` is set, the first allocate answers
/// `queued{ticket:"T1"}` and only the retry (carrying ticket=T1) gets
/// `allocate_body`. Records confirm/fail/routes hit counts.
fn spawn_backend_new_session(
    allocate_status: u16,
    allocate_body: String,
    queue_first: bool,
) -> (String, AllocCounts) {
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let addr = format!("http://{}", server.server_addr());
    let counts = AllocCounts {
        confirms: Arc::new(AtomicUsize::new(0)),
        fails: Arc::new(AtomicUsize::new(0)),
        routes: Arc::new(AtomicUsize::new(0)),
    };
    let c = counts.clone();
    thread::spawn(move || {
        let mut allocate_calls = 0usize;
        for mut req in server.incoming_requests() {
            let url = req.url().to_string();
            let json_header =
                tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
                    .unwrap();
            if url == "/internal/grid/allocate" {
                let mut body = String::new();
                req.as_reader().read_to_string(&mut body).unwrap();
                let v: serde_json::Value = serde_json::from_str(&body).unwrap();
                if queue_first && allocate_calls == 0 {
                    allocate_calls += 1;
                    let resp =
                        tiny_http::Response::from_string(r#"{"status":"queued","ticket":"T1"}"#)
                            .with_header(json_header);
                    req.respond(resp).unwrap();
                    continue;
                }
                if queue_first {
                    assert_eq!(v["ticket"], "T1", "retry must carry the issued ticket");
                }
                allocate_calls += 1;
                let resp = tiny_http::Response::from_string(allocate_body.clone())
                    .with_status_code(allocate_status)
                    .with_header(json_header);
                req.respond(resp).unwrap();
            } else if url == "/internal/grid/sessions/A1/confirm" {
                let mut body = String::new();
                req.as_reader().read_to_string(&mut body).unwrap();
                let v: serde_json::Value = serde_json::from_str(&body).unwrap();
                assert_eq!(v["appium_session_id"], "app-1", "confirm payload");
                c.confirms.fetch_add(1, Ordering::SeqCst);
                req.respond(tiny_http::Response::from_string("{}")).unwrap();
            } else if url == "/internal/grid/sessions/A1/fail" {
                c.fails.fetch_add(1, Ordering::SeqCst);
                req.respond(tiny_http::Response::from_string("{}")).unwrap();
            } else if url == "/internal/grid/routes" {
                c.routes.fetch_add(1, Ordering::SeqCst);
                req.respond(tiny_http::Response::from_string(r#"{"routes":[]}"#))
                    .unwrap();
            } else {
                req.respond(tiny_http::Response::from_string("{}")).unwrap();
            }
        }
    });
    (addr, counts)
}

#[test]
fn new_session_allocates_creates_and_confirms() {
    let appium_addr =
        spawn_appium_new_session(200, r#"{"value":{"sessionId":"app-1","capabilities":{}}}"#);
    let allocate_body =
        serde_json::json!({"status":"allocated","allocation_id":"A1","target":appium_addr,"claim_window_sec":120})
            .to_string();
    let (backend_addr, counts) = spawn_backend_new_session(200, allocate_body, false);
    let router = spawn_router(&backend_addr);
    let base = format!("http://127.0.0.1:{}", router.port);

    // POST /session: response is appium's body byte-identical.
    let body = ureq::post(&format!("{base}/session"))
        .send_string(r#"{"capabilities":{"alwaysMatch":{"platformName":"Android"}}}"#)
        .unwrap()
        .into_string()
        .unwrap();
    assert_eq!(body, r#"{"value":{"sessionId":"app-1","capabilities":{}}}"#);
    assert_eq!(counts.confirms.load(Ordering::SeqCst), 1, "must confirm");

    // Subsequent command proxies WITHOUT a /routes fetch (route inserted).
    let routes_before = counts.routes.load(Ordering::SeqCst);
    let body = ureq::post(&format!("{base}/session/app-1/url"))
        .send_string(r#"{"url":"http://example.com"}"#)
        .unwrap()
        .into_string()
        .unwrap();
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["upstream"], "appium", "got: {body}");
    assert_eq!(
        counts.routes.load(Ordering::SeqCst),
        routes_before,
        "route was inserted, no rebuild expected"
    );
}

#[test]
fn new_session_queue_then_allocated() {
    let appium_addr =
        spawn_appium_new_session(200, r#"{"value":{"sessionId":"app-1","capabilities":{}}}"#);
    let allocate_body =
        serde_json::json!({"status":"allocated","allocation_id":"A1","target":appium_addr,"claim_window_sec":120})
            .to_string();
    let (backend_addr, counts) = spawn_backend_new_session(200, allocate_body, true);
    let router = spawn_router(&backend_addr);
    let base = format!("http://127.0.0.1:{}", router.port);

    let body = ureq::post(&format!("{base}/session"))
        .send_string(r#"{"capabilities":{"alwaysMatch":{"platformName":"Android"}}}"#)
        .unwrap()
        .into_string()
        .unwrap();
    assert_eq!(body, r#"{"value":{"sessionId":"app-1","capabilities":{}}}"#);
    assert_eq!(counts.confirms.load(Ordering::SeqCst), 1, "must confirm");
}

#[test]
fn new_session_no_match_returns_w3c_error() {
    // Appium is never reached; allocate answers 410.
    let appium_addr = spawn_appium_new_session(200, r#"{"value":{"sessionId":"app-1"}}"#);
    let _ = &appium_addr;
    let (backend_addr, _counts) = spawn_backend_new_session(410, String::new(), false);
    let router = spawn_router(&backend_addr);
    let base = format!("http://127.0.0.1:{}", router.port);

    let err = ureq::post(&format!("{base}/session"))
        .send_string(r#"{"capabilities":{"alwaysMatch":{"platformName":"Android"}}}"#);
    match err {
        Err(ureq::Error::Status(500, resp)) => {
            let body = resp.into_string().unwrap();
            let v: serde_json::Value = serde_json::from_str(&body).unwrap();
            assert_eq!(v["value"]["error"], "session not created", "got: {body}");
        }
        other => panic!("expected 500, got {other:?}"),
    }
}

#[test]
fn new_session_appium_failure_reports_fail() {
    let appium_addr = spawn_appium_new_session(
        500,
        r#"{"value":{"error":"session not created","message":"boom","stacktrace":""}}"#,
    );
    let allocate_body =
        serde_json::json!({"status":"allocated","allocation_id":"A1","target":appium_addr,"claim_window_sec":120})
            .to_string();
    let (backend_addr, counts) = spawn_backend_new_session(200, allocate_body, false);
    let router = spawn_router(&backend_addr);
    let base = format!("http://127.0.0.1:{}", router.port);

    let err = ureq::post(&format!("{base}/session"))
        .send_string(r#"{"capabilities":{"alwaysMatch":{"platformName":"Android"}}}"#);
    match err {
        Err(ureq::Error::Status(500, resp)) => {
            let body = resp.into_string().unwrap();
            // Appium's error body passes through unchanged.
            assert_eq!(
                body,
                r#"{"value":{"error":"session not created","message":"boom","stacktrace":""}}"#
            );
        }
        other => panic!("expected 500, got {other:?}"),
    }
    assert_eq!(counts.fails.load(Ordering::SeqCst), 1, "must report fail");
}

/// Appium stub for the confirm-rollback flow: POST /session returns
/// `{value:{sessionId:"app-1"}}`; records whether `DELETE /session/app-1` was
/// received (the router's rollback path must issue it). Other paths echo.
fn spawn_appium_rollback() -> (String, Arc<AtomicUsize>) {
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let addr = format!("http://{}", server.server_addr());
    let deletes = Arc::new(AtomicUsize::new(0));
    let d = deletes.clone();
    thread::spawn(move || {
        for mut req in server.incoming_requests() {
            let method = req.method().as_str().to_string();
            let path = req.url().to_string();
            let mut body = String::new();
            req.as_reader().read_to_string(&mut body).unwrap();
            let json_header =
                tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
                    .unwrap();
            if method == "POST" && path == "/session" {
                let resp = tiny_http::Response::from_string(
                    r#"{"value":{"sessionId":"app-1","capabilities":{}}}"#,
                )
                .with_status_code(200)
                .with_header(json_header);
                req.respond(resp).unwrap();
            } else if method == "DELETE" && path == "/session/app-1" {
                d.fetch_add(1, Ordering::SeqCst);
                req.respond(tiny_http::Response::from_string("{}")).unwrap();
            } else {
                let payload = serde_json::json!({
                    "upstream": "appium",
                    "method": method,
                    "path": path,
                    "body": body,
                });
                let resp =
                    tiny_http::Response::from_string(payload.to_string()).with_header(json_header);
                req.respond(resp).unwrap();
            }
        }
    });
    (addr, deletes)
}

/// Backend stub for the confirm-rollback flow: allocate -> allocated(A1),
/// confirm -> 409 (allocation already reaped), routes -> empty.
fn spawn_backend_confirm_409(appium_addr: String) -> String {
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let addr = format!("http://{}", server.server_addr());
    thread::spawn(move || {
        for req in server.incoming_requests() {
            let url = req.url().to_string();
            let json_header =
                tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
                    .unwrap();
            if url == "/internal/grid/allocate" {
                let body = serde_json::json!({
                    "status": "allocated",
                    "allocation_id": "A1",
                    "target": appium_addr,
                    "claim_window_sec": 120,
                })
                .to_string();
                let resp = tiny_http::Response::from_string(body).with_header(json_header);
                req.respond(resp).unwrap();
            } else if url == "/internal/grid/sessions/A1/confirm" {
                let resp = tiny_http::Response::from_string("{}").with_status_code(409);
                req.respond(resp).unwrap();
            } else if url == "/internal/grid/routes" {
                req.respond(tiny_http::Response::from_string(r#"{"routes":[]}"#))
                    .unwrap();
            } else {
                req.respond(tiny_http::Response::from_string("{}")).unwrap();
            }
        }
    });
    addr
}

#[test]
fn new_session_confirm_failure_rolls_back() {
    let (appium_addr, deletes) = spawn_appium_rollback();
    let backend_addr = spawn_backend_confirm_409(appium_addr);
    let router = spawn_router(&backend_addr);
    let base = format!("http://127.0.0.1:{}", router.port);

    // Confirm 409 -> client gets 500 "session not created"; no route inserted.
    let err = ureq::post(&format!("{base}/session"))
        .send_string(r#"{"capabilities":{"alwaysMatch":{"platformName":"Android"}}}"#);
    match err {
        Err(ureq::Error::Status(500, resp)) => {
            let body = resp.into_string().unwrap();
            let v: serde_json::Value = serde_json::from_str(&body).unwrap();
            assert_eq!(v["value"]["error"], "session not created", "got: {body}");
        }
        other => panic!("expected 500, got {other:?}"),
    }

    // The router must have rolled the Appium session back via DELETE.
    assert_eq!(
        deletes.load(Ordering::SeqCst),
        1,
        "router must DELETE the unconfirmed Appium session"
    );

    // No route was inserted: a follow-up command 404s (backend /routes empty).
    let err = ureq::get(&format!("{base}/session/app-1/url")).call();
    match err {
        Err(ureq::Error::Status(404, resp)) => {
            let body = resp.into_string().unwrap();
            let v: serde_json::Value = serde_json::from_str(&body).unwrap();
            assert_eq!(v["value"]["error"], "invalid session id", "got: {body}");
        }
        other => panic!("expected 404, got {other:?}"),
    }
}

/// Raw-TCP WebSocket echo stub (tiny_http cannot perform the HTTP 101 upgrade).
/// Completes the RFC 6455 handshake and echoes one client frame back prefixed
/// with `ws:`, so the test can prove frames splice through the router intact.
fn spawn_ws_echo() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = format!("http://{}", listener.local_addr().unwrap());
    thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(stream) = stream else { continue };
            thread::spawn(move || ws_echo_conn(stream));
        }
    });
    addr
}

fn ws_echo_conn(mut stream: TcpStream) {
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let mut request_line = String::new();
    if reader.read_line(&mut request_line).unwrap_or(0) == 0 {
        return;
    }
    let mut ws_key = None;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line).unwrap_or(0) == 0 {
            return;
        }
        let trimmed = line.trim_end().to_string();
        if trimmed.is_empty() {
            break;
        }
        let lower = trimmed.to_lowercase();
        if let Some(v) = lower.strip_prefix("sec-websocket-key:") {
            let _ = v;
            ws_key = Some(trimmed["sec-websocket-key:".len()..].trim().to_string());
        }
    }
    let Some(key) = ws_key else { return };
    let accept = base64(
        ring::digest::digest(
            &ring::digest::SHA1_FOR_LEGACY_USE_ONLY,
            format!("{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11").as_bytes(),
        )
        .as_ref(),
    );
    let response = format!(
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
    );
    stream.write_all(response.as_bytes()).unwrap();
    // Read one masked client frame (small payload assumed), echo it prefixed.
    let mut hdr = [0u8; 2];
    if reader.read_exact(&mut hdr).is_err() {
        return;
    }
    let len = (hdr[1] & 0x7F) as usize;
    let mut mask = [0u8; 4];
    reader.read_exact(&mut mask).unwrap();
    let mut payload = vec![0u8; len];
    reader.read_exact(&mut payload).unwrap();
    for (i, byte) in payload.iter_mut().enumerate() {
        *byte ^= mask[i % 4];
    }
    let reply = format!("ws:{}", String::from_utf8_lossy(&payload));
    let mut frame = vec![0x81u8, reply.len() as u8];
    frame.extend_from_slice(reply.as_bytes());
    stream.write_all(&frame).unwrap();
}

fn base64(data: &[u8]) -> String {
    const TABLE: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in data.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = u32::from_be_bytes([0, b[0], b[1], b[2]]);
        out.push(TABLE[(n >> 18) as usize & 63] as char);
        out.push(TABLE[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            TABLE[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            TABLE[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

fn ws_roundtrip(router_port: u16, path: &str, message: &str) -> String {
    let mut stream = TcpStream::connect(("127.0.0.1", router_port)).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{router_port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
    );
    stream.write_all(request.as_bytes()).unwrap();
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    // Consume the 101 response headers.
    loop {
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        if line == "\r\n" || line == "\n" {
            break;
        }
        if line.starts_with("HTTP/") {
            assert!(line.contains("101"), "expected 101, got: {line}");
        }
    }
    // Send one masked text frame.
    let mask = [0x11u8, 0x22, 0x33, 0x44];
    let mut frame = vec![0x81u8, 0x80 | message.len() as u8];
    frame.extend_from_slice(&mask);
    frame.extend(message.bytes().enumerate().map(|(i, b)| b ^ mask[i % 4]));
    stream.write_all(&frame).unwrap();
    // Read the unmasked server echo frame.
    let mut hdr = [0u8; 2];
    reader.read_exact(&mut hdr).unwrap();
    let len = (hdr[1] & 0x7F) as usize;
    let mut payload = vec![0u8; len];
    reader.read_exact(&mut payload).unwrap();
    String::from_utf8(payload).unwrap()
}

#[test]
fn proxies_websocket_session_command() {
    // Route a session at a raw-TCP WS echo upstream; the router resolves the
    // route on a cache miss (same lazy-rebuild path as the command test).
    let ws_addr = spawn_ws_echo();
    let (backend_addr, _hits) = spawn_backend(ws_addr);
    let router = spawn_router(&backend_addr);

    // /session/{id}/se/cdp classifies as a SessionCommand, so pingora performs
    // the upgrade against the resolved upstream and splices frames through.
    let reply = ws_roundtrip(router.port, "/session/known-session/se/cdp", "hello-ws");
    assert_eq!(reply, "ws:hello-ws");
}
