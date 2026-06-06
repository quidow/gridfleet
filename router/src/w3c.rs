//! W3C WebDriver error envelopes (the router's only owned response shapes).

pub fn error_body(error: &str, message: &str) -> Vec<u8> {
    serde_json::to_vec(&serde_json::json!({
        "value": {"error": error, "message": message, "stacktrace": ""}
    }))
    .expect("static json")
}

pub fn status_body() -> Vec<u8> {
    serde_json::to_vec(&serde_json::json!({
        "value": {"ready": true, "message": "GridFleet router"}
    }))
    .expect("static json")
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
}
