use std::collections::HashSet;
use std::thread;
use std::time::Duration;

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
async fn create_session_sends_budget_header() {
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let req = server.recv().unwrap();
        let budget = req
            .headers()
            .iter()
            .find(|header| {
                header
                    .field
                    .to_string()
                    .eq_ignore_ascii_case("x-gridfleet-create-budget-ms")
            })
            .expect("create budget header");
        assert_eq!(budget.value.as_str(), "30000");
        req.respond(json_response(r#"{"status":"queued","ticket":"ticket-1"}"#))
            .unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);

    let outcome = client
        .create_session(
            br#"{"capabilities":{}}"#,
            None,
            None,
            Duration::from_secs(30),
        )
        .await
        .unwrap();

    assert!(matches!(
        outcome,
        gridfleet_router::backend::CreateOutcome::Queued { .. }
    ));
}

#[tokio::test]
async fn create_session_created_roundtrip() {
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let mut req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/create-session");
        let mut body = String::new();
        req.as_reader().read_to_string(&mut body).unwrap();
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert!(v["body"]["capabilities"].is_object());
        assert!(v["ticket"].is_null());
        req.respond(json_response(
            r#"{"status":"created","session_id":"sid-1","target":"http://host:4730","device_id":null,"appium_status":200,"appium_body":{"value":{"sessionId":"sid-1"}}}"#,
        ))
        .unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    let raw = br#"{"capabilities":{"alwaysMatch":{"platformName":"Android"}}}"#;
    match client
        .create_session(raw, None, None, Duration::from_secs(30))
        .await
        .unwrap()
    {
        gridfleet_router::backend::CreateOutcome::Created {
            session_id,
            target,
            appium_status,
            ..
        } => {
            assert_eq!(session_id, "sid-1");
            assert_eq!(target, "http://host:4730");
            assert_eq!(appium_status, 200);
        }
        other => panic!("expected Created, got {other:?}"),
    }
}

#[tokio::test]
async fn create_session_forwards_group_capabilities_verbatim() {
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let mut req = server.recv().unwrap();
        let mut body = String::new();
        req.as_reader().read_to_string(&mut body).unwrap();
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        // The router must relay the group capability untouched: same key, and the
        // JSON boolean `true` rather than a stringified or rewritten value.
        assert_eq!(
            v["body"]["capabilities"]["alwaysMatch"],
            serde_json::json!({
                "platformName": "Android",
                "gridfleet:group:east-lab": true,
                "gridfleet:group:screen-type-4k": true,
            })
        );
        req.respond(json_response(
            r#"{"status":"created","session_id":"sid-1","target":"http://host:4730","device_id":null,"appium_status":200,"appium_body":{"value":{"sessionId":"sid-1"}}}"#,
        ))
        .unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    let raw = br#"{"capabilities":{"alwaysMatch":{"platformName":"Android","gridfleet:group:east-lab":true,"gridfleet:group:screen-type-4k":true}}}"#;

    assert!(matches!(
        client
            .create_session(raw, None, None, Duration::from_secs(30))
            .await
            .unwrap(),
        gridfleet_router::backend::CreateOutcome::Created { .. }
    ));
}

#[tokio::test]
async fn create_session_queued_roundtrip() {
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let mut req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/create-session");
        let mut body = String::new();
        req.as_reader().read_to_string(&mut body).unwrap();
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["ticket"], "ticket-1");
        req.respond(json_response(r#"{"status":"queued","ticket":"ticket-2"}"#))
            .unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    match client
        .create_session(
            br#"{"capabilities":{}}"#,
            Some("ticket-1"),
            Some("run-1"),
            Duration::from_secs(30),
        )
        .await
        .unwrap()
    {
        gridfleet_router::backend::CreateOutcome::Queued { ticket } => {
            assert_eq!(ticket, "ticket-2")
        }
        other => panic!("expected Queued, got {other:?}"),
    }
}

#[tokio::test]
async fn create_session_failed_and_error_roundtrip() {
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let req = server.recv().unwrap();
        req.respond(json_response(
            r#"{"status":"create_failed","appium_status":500,"appium_body":{"value":{"error":"session not created"}}}"#,
        ))
        .unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    match client
        .create_session(br#"{}"#, None, None, Duration::from_secs(30))
        .await
        .unwrap()
    {
        gridfleet_router::backend::CreateOutcome::CreateFailed { appium_status, .. } => {
            assert_eq!(appium_status, 500);
        }
        other => panic!("expected CreateFailed, got {other:?}"),
    }

    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let req = server.recv().unwrap();
        req.respond(json_response(
            r#"{"status":"create_error","message":"upstream unreachable"}"#,
        ))
        .unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    match client
        .create_session(br#"{}"#, None, None, Duration::from_secs(30))
        .await
        .unwrap()
    {
        gridfleet_router::backend::CreateOutcome::CreateError { message } => {
            assert!(message.contains("unreachable"));
        }
        other => panic!("expected CreateError, got {other:?}"),
    }
}

#[tokio::test]
async fn create_session_rejections_fail_fast() {
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let req = server.recv().unwrap();
        req.respond(
            tiny_http::Response::from_string(r#"{"status":"invalid","message":"bad caps"}"#)
                .with_status_code(tiny_http::StatusCode(400)),
        )
        .unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    match client
        .create_session(br#"{}"#, None, None, Duration::from_secs(30))
        .await
        .unwrap()
    {
        gridfleet_router::backend::CreateOutcome::Invalid { message } => {
            assert_eq!(message, "bad caps")
        }
        other => panic!("expected Invalid, got {other:?}"),
    }

    let client = gridfleet_router::backend::BackendClient::new("http://127.0.0.1:1", None);
    match client
        .create_session(b"not json", None, None, Duration::from_secs(30))
        .await
        .unwrap()
    {
        gridfleet_router::backend::CreateOutcome::Invalid { message } => {
            assert!(message.contains("invalid JSON"))
        }
        other => panic!("expected Invalid for bad JSON, got {other:?}"),
    }

    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let req = server.recv().unwrap();
        req.respond(tiny_http::Response::empty(tiny_http::StatusCode(410)))
            .unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    assert!(matches!(
        client
            .create_session(br#"{}"#, None, None, Duration::from_secs(30))
            .await
            .unwrap(),
        gridfleet_router::backend::CreateOutcome::QueueTimeout
    ));
}

#[tokio::test]
async fn session_end_cancel_and_activity_use_current_routes() {
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/sessions/ended");
        req.respond(tiny_http::Response::empty(204)).unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    client.session_ended("sid-1").await.unwrap();

    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/tickets/ticket-1");
        assert_eq!(req.method(), &tiny_http::Method::Delete);
        req.respond(tiny_http::Response::empty(204)).unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    client.cancel_ticket("ticket-1").await.unwrap();

    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let mut req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/activity");
        let mut body = String::new();
        req.as_reader().read_to_string(&mut body).unwrap();
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["sessions"], serde_json::json!(["sid-1"]));
        req.respond(tiny_http::Response::empty(204)).unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    let mut sessions = HashSet::new();
    sessions.insert("sid-1".to_string());
    client.flush_activity(sessions).await.unwrap();
}

#[tokio::test]
async fn fetch_routes_roundtrip() {
    let (server, addr) = stub_backend();
    thread::spawn(move || {
        let req = server.recv().unwrap();
        assert_eq!(req.url(), "/internal/grid/routes");
        req.respond(json_response(
            r#"{"routes":[{"session_id":"sid-1","target":"http://host:1"}]}"#,
        ))
        .unwrap();
    });
    let client = gridfleet_router::backend::BackendClient::new(&addr, None);
    assert_eq!(
        client.fetch_routes().await.unwrap(),
        vec![("sid-1".to_string(), "http://host:1".to_string())]
    );
}
