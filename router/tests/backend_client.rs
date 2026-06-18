use std::collections::HashSet;
use std::thread;

fn stub_backend() -> (tiny_http::Server, String) {
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let addr = format!("http://{}", server.server_addr());
    (server, addr)
}

fn json_response(body: &str) -> tiny_http::Response<std::io::Cursor<Vec<u8>>> {
    tiny_http::Response::from_string(body).with_header(
        tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..]).unwrap(),
    )
}

#[tokio::test]
async fn allocate_allocated_roundtrip() {
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let mut req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/allocate");
        let mut body = String::new();
        req.as_reader().read_to_string(&mut body).unwrap();
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert!(v["body"]["capabilities"].is_object());
        assert!(v["ticket"].is_null());
        let resp = r#"{"status":"allocated","allocation_id":"00000000-0000-0000-0000-000000000001","target":"http://10.0.0.5:4723","claim_window_sec":120}"#;
        req.respond(json_response(resp)).unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    let raw = br#"{"capabilities":{"alwaysMatch":{"platformName":"Android"}}}"#;
    match client.allocate(raw, None, None).await.unwrap() {
        gridfleet_router::backend::AllocateOutcome::Allocated {
            allocation_id,
            target,
            claim_window_sec,
            device_id: _,
        } => {
            assert_eq!(allocation_id, "00000000-0000-0000-0000-000000000001");
            assert_eq!(target, "http://10.0.0.5:4723");
            assert_eq!(claim_window_sec, Some(120));
        }
        other => panic!("expected Allocated, got {other:?}"),
    }
}

#[tokio::test]
async fn allocate_allocated_without_claim_window_is_none() {
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/allocate");
        // Older backend omits claim_window_sec entirely.
        let resp = r#"{"status":"allocated","allocation_id":"A","target":"http://10.0.0.5:4723"}"#;
        req.respond(json_response(resp)).unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    let raw = br#"{"capabilities":{"alwaysMatch":{"platformName":"Android"}}}"#;
    match client.allocate(raw, None, None).await.unwrap() {
        gridfleet_router::backend::AllocateOutcome::Allocated {
            claim_window_sec, ..
        } => {
            assert_eq!(claim_window_sec, None);
        }
        other => panic!("expected Allocated, got {other:?}"),
    }
}

#[tokio::test]
async fn allocate_queued_roundtrip() {
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let mut req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/allocate");
        let mut body = String::new();
        req.as_reader().read_to_string(&mut body).unwrap();
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert!(v["body"]["capabilities"].is_object());
        assert_eq!(v["ticket"], "ticket-123");
        let resp = r#"{"status":"queued","ticket":"ticket-456"}"#;
        req.respond(json_response(resp)).unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    let raw = br#"{"capabilities":{"alwaysMatch":{"platformName":"Android"}}}"#;
    match client
        .allocate(raw, Some("ticket-123"), None)
        .await
        .unwrap()
    {
        gridfleet_router::backend::AllocateOutcome::Queued { ticket } => {
            assert_eq!(ticket, "ticket-456");
        }
        other => panic!("expected Queued, got {other:?}"),
    }
}

#[tokio::test]
async fn confirm_ended_fail_activity_roundtrip() {
    // confirm
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let mut req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/sessions/alloc-1/confirm");
        // basic auth must be present
        let has_auth = req
            .headers()
            .iter()
            .any(|h| h.field.equiv("Authorization") && h.value.as_str().starts_with("Basic "));
        assert!(has_auth, "expected basic auth header");
        let mut body = String::new();
        req.as_reader().read_to_string(&mut body).unwrap();
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["appium_session_id"], "appium-xyz");
        assert_eq!(v["appium_capabilities"]["platformName"], "Android");
        req.respond(tiny_http::Response::empty(204)).unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(
        &addr,
        Some(("user".to_string(), "pass".to_string())),
    );
    let caps = serde_json::json!({"platformName": "Android"});
    client
        .confirm("alloc-1", "appium-xyz", Some(&caps))
        .await
        .unwrap();

    // fail
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let mut req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/sessions/alloc-2/fail");
        let mut body = String::new();
        req.as_reader().read_to_string(&mut body).unwrap();
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["message"], "boom");
        req.respond(tiny_http::Response::empty(204)).unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    client.fail("alloc-2", "boom").await.unwrap();

    // session_ended
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let mut req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/sessions/ended");
        let mut body = String::new();
        req.as_reader().read_to_string(&mut body).unwrap();
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["session_id"], "sess-1");
        req.respond(tiny_http::Response::empty(204)).unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    client.session_ended("sess-1").await.unwrap();

    // cancel_ticket
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/allocate/ticket-9");
        assert_eq!(req.method(), &tiny_http::Method::Delete);
        req.respond(tiny_http::Response::empty(204)).unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    client.cancel_ticket("ticket-9").await.unwrap();

    // flush_activity
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let mut req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/activity");
        let mut body = String::new();
        req.as_reader().read_to_string(&mut body).unwrap();
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        // Bare id list (wave-5 #12): the backend stamps server-side now() per id.
        assert_eq!(v["sessions"], serde_json::json!(["sess-a"]));
        req.respond(tiny_http::Response::empty(204)).unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    let mut sessions: HashSet<String> = HashSet::new();
    sessions.insert("sess-a".to_string());
    client.flush_activity(sessions).await.unwrap();

    // flush_activity empty -> no request
    let client = gridfleet_router::backend::BackendClient::new("http://127.0.0.1:1", None);
    client.flush_activity(HashSet::new()).await.unwrap();
}

#[tokio::test]
async fn allocate_400_returns_invalid() {
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/allocate");
        // Backend 400s carry a JSON envelope; the client extracts `message`.
        req.respond(
            tiny_http::Response::from_string(r#"{"status":"invalid","message":"bad caps"}"#)
                .with_status_code(tiny_http::StatusCode(400)),
        )
        .unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    let raw = br#"{"capabilities":{"alwaysMatch":{"platformName":"Android"}}}"#;
    match client.allocate(raw, None, None).await.unwrap() {
        gridfleet_router::backend::AllocateOutcome::Invalid { message } => {
            assert_eq!(message, "bad caps");
        }
        other => panic!("expected Invalid, got {other:?}"),
    }

    // invalid JSON short-circuits before sending any request — use an unreachable port
    let client = gridfleet_router::backend::BackendClient::new("http://127.0.0.1:1", None);
    match client.allocate(b"not json", None, None).await.unwrap() {
        gridfleet_router::backend::AllocateOutcome::Invalid { message } => {
            assert!(message.contains("invalid JSON"), "message was: {message}");
        }
        other => panic!("expected Invalid for bad JSON, got {other:?}"),
    }
}

#[tokio::test]
async fn allocate_400_non_json_body_falls_back_to_raw() {
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let req = server.recv().unwrap();
        req.respond(
            tiny_http::Response::from_string("plain crash dump")
                .with_status_code(tiny_http::StatusCode(400)),
        )
        .unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    let raw = br#"{"capabilities":{"alwaysMatch":{"platformName":"Android"}}}"#;
    match client.allocate(raw, None, None).await.unwrap() {
        gridfleet_router::backend::AllocateOutcome::Invalid { message } => {
            assert_eq!(message, "plain crash dump");
        }
        other => panic!("expected Invalid, got {other:?}"),
    }
}

#[tokio::test]
async fn allocate_410_returns_queue_timeout() {
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/allocate");
        req.respond(tiny_http::Response::empty(tiny_http::StatusCode(410)))
            .unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    let raw = br#"{"capabilities":{"alwaysMatch":{"platformName":"Android"}}}"#;
    match client.allocate(raw, None, None).await.unwrap() {
        gridfleet_router::backend::AllocateOutcome::QueueTimeout => {}
        other => panic!("expected QueueTimeout, got {other:?}"),
    }
}

#[tokio::test]
async fn allocate_unexpected_4xx_is_invalid_fail_fast() {
    // Wave-5 #5: an unexpected 4xx (e.g. FastAPI 422 on a malformed allocate
    // envelope) is a request-level rejection retrying cannot fix. It must map to
    // Invalid (immediate 400 to the client with the body's detail), not an Err the
    // new-session loop retries for the full 330s deadline masking the real error.
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/allocate");
        let resp = json_response(
            r#"{"detail":[{"type":"model_attributes_type","msg":"Input should be a valid dictionary"}]}"#,
        )
        .with_status_code(422);
        req.respond(resp).unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    // Valid JSON but not an object — passes the router's parse check, 422s on the
    // backend's AllocateRequest model.
    let raw = br#"[]"#;
    match client.allocate(raw, None, None).await.unwrap() {
        gridfleet_router::backend::AllocateOutcome::Invalid { message } => {
            assert!(
                message.contains("422"),
                "message should carry the status: {message}"
            );
            assert!(
                message.contains("valid dictionary"),
                "message should carry the backend detail: {message}"
            );
            // Re-review B1: the FastAPI detail array must be rendered as its
            // msg text, not serialized JSON dumped into the message.
            assert!(
                !message.contains('{'),
                "message should be human-readable, not raw JSON: {message}"
            );
        }
        other => panic!("expected Invalid, got {other:?}"),
    }
}

#[tokio::test]
async fn fetch_routes_roundtrip() {
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/routes");
        assert_eq!(req.method(), &tiny_http::Method::Get);
        let resp = r#"{"routes":[{"session_id":"s","target":"http://h:1"}]}"#;
        req.respond(json_response(resp)).unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    let routes = client.fetch_routes().await.unwrap();
    assert_eq!(routes, vec![("s".to_string(), "http://h:1".to_string())]);
}
