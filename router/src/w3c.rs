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
}
