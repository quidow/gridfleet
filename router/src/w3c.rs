//! W3C WebDriver error envelopes (the router's only owned response shapes).

pub fn error_body(error: &str, message: &str) -> Vec<u8> {
    serde_json::to_vec(&serde_json::json!({
        "value": {"error": error, "message": message, "stacktrace": ""}
    }))
    .expect("static json")
}

pub fn status_body() -> Vec<u8> {
    // The body is static; serialize once and hand out clones (wave-5 #24).
    static BODY: std::sync::OnceLock<Vec<u8>> = std::sync::OnceLock::new();
    BODY.get_or_init(|| {
        serde_json::to_vec(&serde_json::json!({
            "value": {"ready": true, "message": "GridFleet router"}
        }))
        .expect("static json")
    })
    .clone()
}

/// Session ids from an Appium list-sessions body (`GET /appium/sessions` or
/// legacy `GET /sessions`): `{"value":[{"id":"..."}]}`. Tolerates `sessionId`
/// as the key. Returns an empty vec for any unexpected shape.
pub fn extract_session_ids(body: &[u8]) -> Vec<String> {
    let Ok(v) = serde_json::from_slice::<serde_json::Value>(body) else {
        return Vec::new();
    };
    v["value"]
        .as_array()
        .map(|arr| {
            arr.iter()
                .filter_map(|s| s["id"].as_str().or_else(|| s["sessionId"].as_str()))
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

/// value.sessionId per W3C; tolerate legacy top-level sessionId.
pub fn extract_session_id(body: &[u8]) -> Option<String> {
    let v: serde_json::Value = serde_json::from_slice(body).ok()?;
    v["value"]["sessionId"]
        .as_str()
        .or_else(|| v["sessionId"].as_str())
        .map(str::to_string)
}

/// value.capabilities from a W3C create-session response. `None` for any
/// unexpected shape — capabilities capture must never fail a session create.
pub fn extract_session_capabilities(body: &[u8]) -> Option<serde_json::Value> {
    let v: serde_json::Value = serde_json::from_slice(body).ok()?;
    let caps = v["value"]["capabilities"].clone();
    caps.is_object().then_some(caps)
}

/// Inject `appium:gridfleet:deviceId` into a W3C create-session response's
/// `value.capabilities`. Best-effort: returns `body` unchanged on any
/// unexpected shape — injection must never fail a session create.
pub fn inject_device_id(body: &[u8], device_id: &str) -> Vec<u8> {
    let Ok(mut v) = serde_json::from_slice::<serde_json::Value>(body) else {
        return body.to_vec();
    };
    let Some(caps) = v["value"]["capabilities"].as_object_mut() else {
        return body.to_vec();
    };
    caps.insert(
        "appium:gridfleet:deviceId".to_string(),
        serde_json::Value::String(device_id.to_string()),
    );
    serde_json::to_vec(&v).unwrap_or_else(|_| body.to_vec())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_is_ready() {
        let v: serde_json::Value = serde_json::from_slice(&status_body()).unwrap();
        assert_eq!(v["value"]["ready"], true);
        assert_eq!(v["value"]["message"], "GridFleet router");
    }

    #[test]
    fn error_bodies() {
        let b = error_body("invalid session id", "no route for session abc");
        let v: serde_json::Value = serde_json::from_slice(&b).unwrap();
        assert_eq!(v["value"]["error"], "invalid session id");
        assert_eq!(v["value"]["message"], "no route for session abc");
        assert_eq!(v["value"]["stacktrace"], "");
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

    #[test]
    fn extract_session_capabilities_w3c_shape() {
        let body = br#"{"value":{"sessionId":"app-1","capabilities":{"platformName":"Android"}}}"#;
        let caps = extract_session_capabilities(body).expect("caps");
        assert_eq!(caps["platformName"], "Android");
    }

    #[test]
    fn extract_session_capabilities_garbage_is_none() {
        assert!(extract_session_capabilities(b"not json").is_none());
        assert!(extract_session_capabilities(br#"{"value":{"sessionId":"x"}}"#).is_none());
        assert!(
            extract_session_capabilities(br#"{"value":{"capabilities":"not-an-object"}}"#)
                .is_none()
        );
    }

    #[test]
    fn extract_session_ids_appium_shape() {
        let body = br#"{"value":[{"id":"s1","capabilities":{}},{"id":"s2"}]}"#;
        assert_eq!(extract_session_ids(body), vec!["s1", "s2"]);
    }

    #[test]
    fn extract_session_ids_tolerates_session_id_key() {
        let body = br#"{"value":[{"sessionId":"s9"}]}"#;
        assert_eq!(extract_session_ids(body), vec!["s9"]);
    }

    #[test]
    fn extract_session_ids_unexpected_shapes_empty() {
        assert!(extract_session_ids(b"not json").is_empty());
        assert!(extract_session_ids(br#"{"value":{}}"#).is_empty());
        assert!(extract_session_ids(br#"{"value":[]}"#).is_empty());
        assert!(extract_session_ids(br#"{"value":[{"foo":"bar"}]}"#).is_empty());
    }

    #[test]
    fn inject_device_id_adds_cap_and_preserves_others() {
        let body = br#"{"value":{"sessionId":"app-1","capabilities":{"platformName":"Android"}}}"#;
        let out = inject_device_id(body, "dev-uuid-1");
        let v: serde_json::Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(
            v["value"]["capabilities"]["appium:gridfleet:deviceId"],
            "dev-uuid-1"
        );
        assert_eq!(v["value"]["capabilities"]["platformName"], "Android");
        assert_eq!(v["value"]["sessionId"], "app-1");
    }

    #[test]
    fn inject_device_id_missing_caps_object_is_unchanged() {
        let body = br#"{"value":{"sessionId":"app-1"}}"#;
        assert_eq!(inject_device_id(body, "dev-uuid-1"), body.to_vec());
    }

    #[test]
    fn inject_device_id_garbage_body_is_unchanged() {
        let body = b"not json";
        assert_eq!(inject_device_id(body, "dev-uuid-1"), body.to_vec());
    }
}
