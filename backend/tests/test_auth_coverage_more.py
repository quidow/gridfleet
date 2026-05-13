import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from starlette.datastructures import Headers

from app.services import auth


def _enable_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth.settings, "auth_enabled", True)
    monkeypatch.setattr(auth.settings, "auth_username", "operator")
    monkeypatch.setattr(auth.settings, "auth_password", "operator-secret")
    monkeypatch.setattr(auth.settings, "auth_session_secret", "session-secret")
    monkeypatch.setattr(auth.settings, "auth_session_ttl_sec", 60)
    monkeypatch.setattr(auth.settings, "auth_cookie_secure", False)
    monkeypatch.setattr(auth.settings, "machine_auth_username", "machine")
    monkeypatch.setattr(auth.settings, "machine_auth_password", "machine-secret")


def _basic(value: bytes) -> Headers:
    return Headers({"authorization": f"Basic {base64.b64encode(value).decode('ascii')}"})


def test_validate_process_configuration_and_path_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth.settings, "auth_enabled", False)
    auth.validate_process_configuration()
    assert auth.is_protected_path("/api/auth/login") is False
    assert auth.is_protected_path("/health/live") is False
    assert auth.is_protected_path("/metrics") is True
    assert auth.is_protected_path("/docs/oauth2-redirect") is True
    assert auth.requires_csrf_check("/api/devices", "get") is False
    assert auth.requires_csrf_check("/api/auth/login", "POST") is False
    assert auth.requires_csrf_check("/api/devices", "DELETE") is True

    _enable_auth(monkeypatch)
    monkeypatch.setattr(auth.settings, "auth_password", "")
    monkeypatch.setattr(auth.settings, "machine_auth_password", "")
    with pytest.raises(RuntimeError, match="GRIDFLEET_AUTH_PASSWORD"):
        auth.validate_process_configuration()


def test_session_decode_and_browser_session_reject_invalid_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_auth(monkeypatch)
    token, _ = auth.issue_session()
    version, encoded, signature = token.split(".", 2)
    assert version == "v1"

    assert auth._decode_session_payload("bad-token") is None
    assert auth._decode_session_payload(f"v2.{encoded}.{signature}") is None
    assert auth._decode_session_payload(f"v1.{encoded}.wrong") is None
    assert auth._decode_session_payload(f"v1.not-base64.{signature}") is None

    list_payload = auth._base64url_encode(json.dumps(["not", "dict"]).encode())
    assert auth._decode_session_payload(f"v1.{list_payload}.{auth._sign_payload(list_payload)}") is None

    missing_fields = auth._base64url_encode(json.dumps({"sub": "operator"}).encode())
    missing_token = f"v1.{missing_fields}.{auth._sign_payload(missing_fields)}"
    assert (
        auth.resolve_browser_session_from_headers(
            Headers({"cookie": f"{auth.SESSION_COOKIE_NAME}={missing_token}"})
        ).authenticated
        is False
    )

    expired = auth._base64url_encode(
        json.dumps(
            {
                "sub": "operator",
                "csrf": "token",
                "exp": int((datetime.now(UTC) - timedelta(seconds=1)).timestamp()),
            }
        ).encode()
    )
    expired_token = f"v1.{expired}.{auth._sign_payload(expired)}"
    assert (
        auth.resolve_browser_session_from_headers(
            Headers({"cookie": f"{auth.SESSION_COOKIE_NAME}={expired_token}"})
        ).authenticated
        is False
    )

    wrong_user = auth._base64url_encode(
        json.dumps(
            {
                "sub": "other",
                "csrf": "token",
                "exp": int((datetime.now(UTC) + timedelta(minutes=1)).timestamp()),
            }
        ).encode()
    )
    wrong_user_token = f"v1.{wrong_user}.{auth._sign_payload(wrong_user)}"
    assert (
        auth.resolve_browser_session_from_headers(
            Headers({"cookie": f"{auth.SESSION_COOKIE_NAME}={wrong_user_token}"})
        ).authenticated
        is False
    )


def test_request_auth_basic_cookie_and_csrf_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth.settings, "auth_enabled", False)
    assert auth.resolve_browser_session_from_headers(Headers()).enabled is False
    assert auth.resolve_request_auth(Headers()).mode == "disabled"
    assert auth._authenticate_basic_auth(Headers({"authorization": "Basic whatever"})) is None

    _enable_auth(monkeypatch)
    assert auth.resolve_browser_session_from_headers(Headers()).authenticated is False
    assert auth.resolve_request_auth(Headers()).mode == "unauthenticated"
    assert auth.require_valid_csrf(Headers(), None) is False
    assert auth.require_valid_csrf(Headers(), "csrf") is False
    assert auth.require_valid_csrf(Headers({"x-csrf-token": "csrf"}), "csrf") is True
    assert auth.authenticate_operator("operator", "operator-secret") is True
    assert auth.authenticate_operator("operator", "bad") is False

    assert auth._authenticate_basic_auth(Headers({"authorization": "Bearer token"})) is None
    assert auth._authenticate_basic_auth(Headers({"authorization": "Basic"})) is None
    assert auth._authenticate_basic_auth(_basic(b"\xff\xff")) is None
    assert auth._authenticate_basic_auth(_basic(b"machine-only")) is None
    assert auth._authenticate_basic_auth(_basic(b"machine:bad")) is None
    assert auth._authenticate_basic_auth(_basic(b"machine:machine-secret")) == "machine"
    assert auth.resolve_request_auth(_basic(b"machine:machine-secret")).mode == "machine"

    token, issued = auth.issue_session()
    browser_result = auth.resolve_request_auth(Headers({"cookie": f"{auth.SESSION_COOKIE_NAME}={token}"}))
    assert browser_result.mode == "browser"
    assert browser_result.username == "operator"
    assert browser_result.csrf_token == issued.csrf_token

    assert auth._read_cookie(Headers({"cookie": "not a valid cookie;"}), auth.SESSION_COOKIE_NAME) is None
    assert auth._read_cookie(Headers({"cookie": "other=value"}), auth.SESSION_COOKIE_NAME) is None
