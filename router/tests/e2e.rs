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

/// ureq 3 strips the response off `Error::StatusCode`, so error-path asserts
/// that inspect the body need `http_status_as_error(false)`.
fn get_any_status(url: &str) -> ureq::http::Response<ureq::Body> {
    ureq::get(url)
        .config()
        .http_status_as_error(false)
        .build()
        .call()
        .unwrap()
}

fn post_any_status(url: &str, body: &str) -> ureq::http::Response<ureq::Body> {
    ureq::post(url)
        .config()
        .http_status_as_error(false)
        .build()
        .send(body)
        .unwrap()
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
        .body_mut()
        .read_to_string()
        .unwrap();
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["value"]["ready"], true, "got: {body}");

    // Known session command -> proxied to stub appium (cache miss triggers one
    // rebuild, then the route resolves).
    let body = ureq::post(&format!("{base}/session/known-session/url"))
        .send(r#"{"url":"http://example.com"}"#)
        .unwrap()
        .body_mut()
        .read_to_string()
        .unwrap();
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["upstream"], "appium", "got: {body}");
    assert_eq!(v["path"], "/session/known-session/url", "got: {body}");
    let hits_after_known = routes_hits.load(Ordering::SeqCst);
    assert!(hits_after_known >= 1, "expected a routes rebuild on miss");

    // Unknown session -> 404 invalid session id, AND a fresh routes refresh.
    let mut resp = get_any_status(&format!("{base}/session/unknown/url"));
    assert_eq!(resp.status(), 404, "expected 404");
    let body = resp.body_mut().read_to_string().unwrap();
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["value"]["error"], "invalid session id", "got: {body}");
    assert!(
        routes_hits.load(Ordering::SeqCst) > hits_after_known,
        "router must refresh routes once before answering 404"
    );

    // Unknown command -> 404 unknown command.
    let mut resp = get_any_status(&format!("{base}/nonsense"));
    assert_eq!(resp.status(), 404, "expected 404");
    let body = resp.body_mut().read_to_string().unwrap();
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["value"]["error"], "unknown command", "got: {body}");
}

#[test]
fn serves_metrics() {
    let appium_addr = spawn_appium();
    let (backend_addr, _hits) = spawn_backend(appium_addr);
    let router = spawn_router(&backend_addr);
    let base = format!("http://127.0.0.1:{}", router.port);

    let mut resp = ureq::get(&format!("{base}/metrics")).call().unwrap();
    assert_eq!(resp.status(), 200);
    let body = resp.body_mut().read_to_string().unwrap();
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
        .send(payload.as_str())
        .unwrap()
        .body_mut()
        .read_to_string()
        .unwrap();
    let v: serde_json::Value = serde_json::from_str(&resp).unwrap();
    let echoed = v["body"].as_str().unwrap();
    assert_eq!(echoed.len(), payload.len(), "large body truncated");
}

/// Appium stub used only for post-create command proxying.
fn spawn_appium_create_counter() -> (String, Arc<AtomicUsize>) {
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let addr = format!("http://{}", server.server_addr());
    let creates = Arc::new(AtomicUsize::new(0));
    let seen = creates.clone();
    thread::spawn(move || {
        for mut req in server.incoming_requests() {
            let method = req.method().as_str().to_string();
            let path = req.url().to_string();
            let mut body = String::new();
            req.as_reader().read_to_string(&mut body).unwrap();
            if method == "POST" && path == "/session" {
                seen.fetch_add(1, Ordering::SeqCst);
            }
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
    (addr, creates)
}

#[derive(Clone)]
struct CreateCounts {
    calls: Arc<AtomicUsize>,
    run_id: Arc<std::sync::Mutex<Option<String>>>,
}

fn spawn_backend_create_session(
    _target: String,
    response_status: u16,
    response_body: String,
    queue_first: bool,
) -> (String, CreateCounts) {
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let addr = format!("http://{}", server.server_addr());
    let counts = CreateCounts {
        calls: Arc::new(AtomicUsize::new(0)),
        run_id: Arc::new(std::sync::Mutex::new(None)),
    };
    let c = counts.clone();
    thread::spawn(move || {
        for mut req in server.incoming_requests() {
            let url = req.url().to_string();
            let json_header =
                tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
                    .unwrap();
            if req.method() == &tiny_http::Method::Post && url == "/internal/grid/create-session" {
                let mut body = String::new();
                req.as_reader().read_to_string(&mut body).unwrap();
                let v: serde_json::Value = serde_json::from_str(&body).unwrap();
                if let Some(run_id) = v["run_id"].as_str() {
                    *c.run_id.lock().unwrap() = Some(run_id.to_string());
                }
                let call = c.calls.fetch_add(1, Ordering::SeqCst);
                if queue_first && call == 0 {
                    req.respond(
                        tiny_http::Response::from_string(
                            r#"{"status":"queued","ticket":"ticket-1"}"#,
                        )
                        .with_header(json_header),
                    )
                    .unwrap();
                } else {
                    if queue_first {
                        assert_eq!(v["ticket"], "ticket-1");
                    }
                    req.respond(
                        tiny_http::Response::from_string(response_body.clone())
                            .with_status_code(response_status)
                            .with_header(json_header),
                    )
                    .unwrap();
                }
            } else if url == "/internal/grid/routes" {
                req.respond(tiny_http::Response::from_string(r#"{"routes":[]}"#))
                    .unwrap();
            } else {
                req.respond(tiny_http::Response::from_string("{}")).unwrap();
            }
        }
    });
    (addr, counts)
}

fn created_body(target: &str) -> String {
    serde_json::json!({
        "status": "created",
        "session_id": "session-1",
        "target": target,
        "device_id": null,
        "appium_status": 200,
        "appium_body": {"value": {"sessionId": "session-1", "capabilities": {}}},
    })
    .to_string()
}

#[test]
fn new_session_backend_owns_creation_and_router_relays() {
    let (appium_addr, appium_creates) = spawn_appium_create_counter();
    let (backend_addr, counts) =
        spawn_backend_create_session(appium_addr.clone(), 200, created_body(&appium_addr), false);
    let router = spawn_router(&backend_addr);
    let base = format!("http://127.0.0.1:{}", router.port);

    let body = ureq::post(&format!("{base}/session"))
        .send(r#"{"capabilities":{"alwaysMatch":{"platformName":"Android"}}}"#)
        .unwrap()
        .body_mut()
        .read_to_string()
        .unwrap();
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&body).unwrap(),
        serde_json::json!({"value":{"sessionId":"session-1","capabilities":{}}})
    );
    assert_eq!(counts.calls.load(Ordering::SeqCst), 1);
    assert_eq!(
        appium_creates.load(Ordering::SeqCst),
        0,
        "router must not create on Appium"
    );

    let command = ureq::post(&format!("{base}/session/session-1/url"))
        .send(r#"{"url":"http://example.com"}"#)
        .unwrap()
        .body_mut()
        .read_to_string()
        .unwrap();
    let v: serde_json::Value = serde_json::from_str(&command).unwrap();
    assert_eq!(v["upstream"], "appium");
    assert_eq!(v["path"], "/session/session-1/url");
}

#[test]
fn new_session_queue_then_created() {
    let appium_addr = spawn_appium();
    let body = created_body(&appium_addr);
    let (backend_addr, counts) = spawn_backend_create_session(appium_addr, 200, body, true);
    let router = spawn_router(&backend_addr);
    let base = format!("http://127.0.0.1:{}", router.port);

    let body = ureq::post(&format!("{base}/session"))
        .send(r#"{"capabilities":{"alwaysMatch":{"platformName":"Android"}}}"#)
        .unwrap()
        .body_mut()
        .read_to_string()
        .unwrap();
    assert!(body.contains("session-1"), "got: {body}");
    assert_eq!(counts.calls.load(Ordering::SeqCst), 2);
}

#[test]
fn new_session_create_failed_relays_appium_envelope() {
    let appium_addr = spawn_appium();
    let failed = serde_json::json!({
        "status": "create_failed",
        "appium_status": 500,
        "appium_body": {"value": {"error": "session not created", "message": "rejected"}},
    })
    .to_string();
    let (backend_addr, _counts) = spawn_backend_create_session(appium_addr, 200, failed, false);
    let router = spawn_router(&backend_addr);
    let base = format!("http://127.0.0.1:{}", router.port);

    let mut resp = post_any_status(
        &format!("{base}/session"),
        r#"{"capabilities":{"alwaysMatch":{"platformName":"Android"}}}"#,
    );
    assert_eq!(resp.status(), 500);
    let body = resp.body_mut().read_to_string().unwrap();
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["value"]["error"], "session not created");
}

#[test]
fn new_session_backend_error_maps_to_w3c_error() {
    let (backend_addr, _counts) = spawn_backend_create_session(
        "http://127.0.0.1:1".to_string(),
        200,
        r#"{"status":"create_error","message":"upstream unreachable"}"#.to_string(),
        false,
    );
    let router = spawn_router(&backend_addr);
    let base = format!("http://127.0.0.1:{}", router.port);

    let mut resp = post_any_status(
        &format!("{base}/session"),
        r#"{"capabilities":{"alwaysMatch":{"platformName":"Android"}}}"#,
    );
    assert_eq!(resp.status(), 500);
    let body = resp.body_mut().read_to_string().unwrap();
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["value"]["error"], "session not created");
    assert!(v["value"]["message"]
        .as_str()
        .unwrap()
        .contains("unreachable"));
}

#[test]
fn new_session_run_prefix_binds_request() {
    let appium_addr = spawn_appium();
    let (backend_addr, counts) =
        spawn_backend_create_session(appium_addr.clone(), 200, created_body(&appium_addr), false);
    let router = spawn_router(&backend_addr);
    let base = format!("http://127.0.0.1:{}", router.port);
    let run_id = "00000000-0000-0000-0000-000000000001";

    let _ = ureq::post(&format!("{base}/run/{run_id}/session"))
        .send(r#"{"capabilities":{"alwaysMatch":{"platformName":"Android"}}}"#)
        .unwrap()
        .body_mut()
        .read_to_string()
        .unwrap();
    assert_eq!(counts.run_id.lock().unwrap().as_deref(), Some(run_id));
}

/// Backend stub for the DELETE-against-dead-upstream flow (F4). Serves
/// `/internal/grid/routes` with a single session pointing at `dead_target`
/// (a port nothing listens on) and records `/internal/grid/sessions/ended`
/// hits. After a failed DELETE the router must still notify session_ended and
/// prune the route so a follow-up command 404s locally.
fn spawn_backend_dead_route(dead_target: String) -> (String, Arc<AtomicUsize>) {
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let addr = format!("http://{}", server.server_addr());
    let ended = Arc::new(AtomicUsize::new(0));
    let e = ended.clone();
    thread::spawn(move || {
        // Serve the dead route only until the first session_ended arrives, so
        // the post-prune /routes refetch on the follow-up command returns empty
        // and the router answers 404 (route genuinely gone).
        for mut req in server.incoming_requests() {
            let url = req.url().to_string();
            let json_header =
                tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
                    .unwrap();
            if url == "/internal/grid/routes" {
                let routes = if e.load(Ordering::SeqCst) == 0 {
                    serde_json::json!({
                        "routes": [{"session_id": "dead-session", "target": dead_target}],
                    })
                } else {
                    serde_json::json!({ "routes": [] })
                };
                let resp =
                    tiny_http::Response::from_string(routes.to_string()).with_header(json_header);
                req.respond(resp).unwrap();
            } else if url == "/internal/grid/sessions/ended" {
                let mut body = String::new();
                req.as_reader().read_to_string(&mut body).unwrap();
                let v: serde_json::Value = serde_json::from_str(&body).unwrap();
                assert_eq!(v["session_id"], "dead-session", "session_ended payload");
                e.fetch_add(1, Ordering::SeqCst);
                req.respond(tiny_http::Response::from_string("{}")).unwrap();
            } else {
                req.respond(tiny_http::Response::from_string("{}")).unwrap();
            }
        }
    });
    (addr, ended)
}

/// A `http://` target that holds its port for the whole test but refuses to
/// serve it: a dedicated thread accepts each inbound connection and drops it
/// immediately, so proxying to it fails at transport (reset/EOF before any HTTP
/// response). The router treats that the same as connection-refused — the
/// `logging()` hook still fires `session_ended` on any DELETE upstream error.
///
/// Holding the port is the point. The old "bind to :0, read the port, release
/// it" trick left the port free, so under cargo's parallel test execution
/// another test could re-bind it and turn the "dead" upstream live — the DELETE
/// then unexpectedly succeeded (flaky on loaded CI runners).
fn dead_http_target() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    thread::spawn(move || {
        for conn in listener.incoming() {
            // Drop each accepted connection at once: the proxy's upstream read
            // sees a reset/EOF instead of a response.
            drop(conn);
        }
    });
    format!("http://127.0.0.1:{port}")
}

#[test]
fn delete_against_dead_upstream_still_notifies_ended() {
    let dead_target = dead_http_target();
    let (backend_addr, ended) = spawn_backend_dead_route(dead_target);
    let router = spawn_router(&backend_addr);
    let base = format!("http://127.0.0.1:{}", router.port);

    // DELETE the session: the upstream is dead, so the proxy fails at transport
    // and the client sees an error (no clean 2xx). The router must still fire
    // session_ended and prune the route.
    let result = ureq::delete(&format!("{base}/session/dead-session")).call();
    assert!(
        result.is_err(),
        "DELETE to a dead upstream should not succeed, got: {result:?}"
    );

    // Wait for the fire-and-forget session_ended notify to land.
    let deadline = Instant::now() + Duration::from_secs(5);
    while ended.load(Ordering::SeqCst) == 0 {
        assert!(
            Instant::now() < deadline,
            "router never notified session_ended after a failed DELETE"
        );
        thread::sleep(Duration::from_millis(25));
    }

    // The route was pruned: a follow-up command 404s locally (the post-prune
    // /routes refetch returns empty).
    let mut resp = get_any_status(&format!("{base}/session/dead-session/url"));
    assert_eq!(resp.status(), 404, "expected 404 after prune");
    let body = resp.body_mut().read_to_string().unwrap();
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["value"]["error"], "invalid session id", "got: {body}");
}

/// Raw-TCP upstream stub speaking the HTTP/1.1 upgrade handshake: replies 101
/// Switching Protocols to an Upgrade request, then echoes every byte it reads.
/// Echoed bytes prove the router established a duplex tunnel, not a plain
/// request/response exchange.
fn spawn_ws_echo_upstream() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = format!("http://{}", listener.local_addr().unwrap());
    thread::spawn(move || {
        for stream in listener.incoming() {
            let stream = match stream {
                Ok(s) => s,
                Err(_) => continue,
            };
            thread::spawn(move || {
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut head = String::new();
                loop {
                    let mut line = String::new();
                    if reader.read_line(&mut line).unwrap_or(0) == 0 {
                        return;
                    }
                    if line == "\r\n" {
                        break;
                    }
                    head.push_str(&line);
                }
                if !head.to_ascii_lowercase().contains("upgrade: websocket") {
                    let mut s = stream;
                    let _ = s.write_all(b"HTTP/1.1 400 Bad Request\r\ncontent-length: 0\r\n\r\n");
                    return;
                }
                let mut stream = stream;
                stream
                    .write_all(
                        b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n",
                    )
                    .unwrap();
                // Duplex echo until either side closes.
                let mut buf = [0u8; 1024];
                loop {
                    match reader.read(&mut buf) {
                        Ok(0) | Err(_) => return,
                        Ok(n) => {
                            if stream.write_all(&buf[..n]).is_err() {
                                return;
                            }
                        }
                    }
                }
            });
        }
    });
    addr
}

#[test]
fn websocket_upgrade_tunnels_through_router() {
    // Wave-5 #2: BiDi/CDP clients pointed at :4444 open a WebSocket on the
    // session path. The router must tunnel the HTTP/1.1 upgrade — 101 relayed
    // downstream, then raw duplex bytes both ways — like the relay's bridge did.
    let ws_upstream = spawn_ws_echo_upstream();
    let (backend_addr, _hits) = spawn_backend(ws_upstream);
    let router = spawn_router(&backend_addr);

    let mut conn = TcpStream::connect(("127.0.0.1", router.port)).unwrap();
    conn.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    conn.write_all(
        b"GET /session/known-session/se/bidi HTTP/1.1\r\n\
          Host: 127.0.0.1\r\n\
          Connection: Upgrade\r\n\
          Upgrade: websocket\r\n\
          Sec-WebSocket-Version: 13\r\n\
          Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n",
    )
    .unwrap();

    let mut reader = BufReader::new(conn.try_clone().unwrap());
    let mut status_line = String::new();
    reader.read_line(&mut status_line).unwrap();
    assert!(
        status_line.starts_with("HTTP/1.1 101"),
        "expected 101 Switching Protocols, got: {status_line:?}"
    );
    loop {
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        if line == "\r\n" {
            break;
        }
    }

    // Duplex proof: bytes sent after the upgrade come back echoed.
    conn.write_all(b"bidi-frame-payload").unwrap();
    let mut echoed = [0u8; 18];
    reader.read_exact(&mut echoed).unwrap();
    assert_eq!(&echoed, b"bidi-frame-payload");

    // And a second round-trip on the same tunnel (not a one-shot body relay).
    conn.write_all(b"second-frame").unwrap();
    let mut echoed2 = [0u8; 12];
    reader.read_exact(&mut echoed2).unwrap();
    assert_eq!(&echoed2, b"second-frame");
}

#[test]
fn websocket_tunnel_survives_idle_beyond_proxy_timeout() {
    // The harness starts the router with --proxy-timeout 5: a BiDi/CDP socket
    // sitting idle between frames (a debugger paused, a slow test) must not be
    // torn down by the per-command upstream read timeout.
    let ws_upstream = spawn_ws_echo_upstream();
    let (backend_addr, _hits) = spawn_backend(ws_upstream);
    let router = spawn_router(&backend_addr);

    let mut conn = TcpStream::connect(("127.0.0.1", router.port)).unwrap();
    conn.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    conn.write_all(
        b"GET /session/known-session/se/bidi HTTP/1.1\r\n\
          Host: 127.0.0.1\r\n\
          Connection: Upgrade\r\n\
          Upgrade: websocket\r\n\
          Sec-WebSocket-Version: 13\r\n\
          Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n",
    )
    .unwrap();

    let mut reader = BufReader::new(conn.try_clone().unwrap());
    let mut status_line = String::new();
    reader.read_line(&mut status_line).unwrap();
    assert!(
        status_line.starts_with("HTTP/1.1 101"),
        "expected 101 Switching Protocols, got: {status_line:?}"
    );
    loop {
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        if line == "\r\n" {
            break;
        }
    }

    // Idle past the router's 5s proxy timeout, then exchange a frame.
    thread::sleep(Duration::from_secs(7));
    conn.write_all(b"late-frame").unwrap();
    let mut echoed = [0u8; 10];
    reader.read_exact(&mut echoed).unwrap();
    assert_eq!(&echoed, b"late-frame");
}

/// Upstream Appium stub that answers 500 to every request (driver hiccup
/// mid-teardown): the DELETE gets a response, but not a 2xx/404.
fn spawn_appium_delete_500() -> String {
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let addr = format!("http://{}", server.server_addr());
    thread::spawn(move || {
        for req in server.incoming_requests() {
            let resp = tiny_http::Response::from_string(
                r#"{"value":{"error":"unknown error","message":"teardown hiccup"}}"#,
            )
            .with_status_code(500)
            .with_header(
                tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
                    .unwrap(),
            );
            req.respond(resp).unwrap();
        }
    });
    addr
}

#[test]
fn delete_answered_5xx_still_notifies_ended_and_prunes() {
    // Wave-5 #4: a client DELETE is unambiguous intent. If Appium answers 5xx
    // mid-teardown, the router must still prune the route and notify
    // session_ended — otherwise the backend `running` row pins the device until
    // the idle timeout (~30 min). A session that actually survived the hiccup is
    // the orphan sweep's job (the closed row's id is a doomed id).
    let appium = spawn_appium_delete_500();
    let (backend_addr, ended) = spawn_backend_dead_route(appium);
    let router = spawn_router(&backend_addr);
    let base = format!("http://127.0.0.1:{}", router.port);

    // The 500 is relayed to the client unchanged.
    match ureq::delete(&format!("{base}/session/dead-session")).call() {
        Err(ureq::Error::StatusCode(500)) => {}
        other => panic!("expected relayed 500, got {other:?}"),
    }

    // session_ended must still fire.
    let deadline = Instant::now() + Duration::from_secs(5);
    while ended.load(Ordering::SeqCst) == 0 {
        assert!(
            Instant::now() < deadline,
            "router never notified session_ended after a 5xx-answered DELETE"
        );
        thread::sleep(Duration::from_millis(25));
    }

    // The route was pruned: a follow-up command 404s locally.
    let mut resp = get_any_status(&format!("{base}/session/dead-session/url"));
    assert_eq!(resp.status(), 404, "expected 404 after prune");
    let body = resp.body_mut().read_to_string().unwrap();
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["value"]["error"], "invalid session id", "got: {body}");
}
