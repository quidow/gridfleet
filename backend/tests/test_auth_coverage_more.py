from datetime import UTC, datetime, timedelta

import jwt as _pyjwt
import pytest
from starlette.datastructures import Headers

from app.auth import service as auth


def _enable_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth.settings, "auth_enabled", True)
    monkeypatch.setattr(auth.settings, "auth_username", "operator")
    monkeypatch.setattr(auth.settings, "auth_password", "operator-secret")
    monkeypatch.setattr(auth.settings, "auth_session_secret", "session-secret-padded-to-32-bytes-min")
    monkeypatch.setattr(auth.settings, "auth_session_ttl_sec", 60)
    monkeypatch.setattr(auth.settings, "auth_cookie_secure", False)
    monkeypatch.setattr(auth.settings, "machine_auth_username", "machine")
    monkeypatch.setattr(auth.settings, "machine_auth_password", "machine-secret")


def test_validate_process_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth.settings, "auth_enabled", False)
    auth.validate_process_configuration()

    _enable_auth(monkeypatch)
    monkeypatch.setattr(auth.settings, "auth_password", "")
    monkeypatch.setattr(auth.settings, "machine_auth_password", "")
    with pytest.raises(RuntimeError, match="GRIDFLEET_AUTH_PASSWORD"):
        auth.validate_process_configuration()


def test_session_decode_and_browser_session_reject_invalid_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_auth(monkeypatch)
    secret = "session-secret-padded-to-32-bytes-min"
    token, _ = auth.issue_session()
    # JWT tokens are 3 base64url segments separated by dots (header.payload.signature)
    parts = token.split(".")
    assert len(parts) == 3

    assert auth._decode_session_payload("bad-token") is None
    assert auth._decode_session_payload("not.a.jwt.at.all") is None
    # Tampered signature
    head, body, sig = token.split(".")
    assert auth._decode_session_payload(f"{head}.{body}.{sig[:-2]}AA") is None

    # Missing required claim (no csrf) → None
    missing_csrf_token = _pyjwt.encode({"sub": "operator", "exp": 9999999999}, secret, algorithm="HS256")
    assert auth._decode_session_payload(missing_csrf_token) is None
    assert (
        auth.resolve_browser_session_from_headers(
            Headers({"cookie": f"{auth.SESSION_COOKIE_NAME}={missing_csrf_token}"})
        ).authenticated
        is False
    )

    # Expired token → None
    expired_token = _pyjwt.encode(
        {
            "sub": "operator",
            "csrf": "token",
            "exp": int((datetime.now(UTC) - timedelta(seconds=1)).timestamp()),
        },
        secret,
        algorithm="HS256",
    )
    assert (
        auth.resolve_browser_session_from_headers(
            Headers({"cookie": f"{auth.SESSION_COOKIE_NAME}={expired_token}"})
        ).authenticated
        is False
    )

    # Wrong username → rejected in resolve_browser_session_from_headers
    wrong_user_token = _pyjwt.encode(
        {
            "sub": "other",
            "csrf": "token",
            "exp": int((datetime.now(UTC) + timedelta(minutes=1)).timestamp()),
        },
        secret,
        algorithm="HS256",
    )
    assert (
        auth.resolve_browser_session_from_headers(
            Headers({"cookie": f"{auth.SESSION_COOKIE_NAME}={wrong_user_token}"})
        ).authenticated
        is False
    )


def test_machine_credentials_and_csrf_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth.settings, "auth_enabled", False)
    assert auth.resolve_browser_session_from_headers(Headers()).enabled is False

    _enable_auth(monkeypatch)
    assert auth.resolve_browser_session_from_headers(Headers()).authenticated is False
    assert auth.require_valid_csrf(Headers(), None) is False
    assert auth.require_valid_csrf(Headers(), "csrf") is False
    assert auth.require_valid_csrf(Headers({"x-csrf-token": "csrf"}), "csrf") is True
    assert auth.authenticate_operator("operator", "operator-secret") is True
    assert auth.authenticate_operator("operator", "bad") is False

    # check_machine_credentials validates username+password without touching headers
    assert auth.check_machine_credentials("machine", "bad") is None
    assert auth.check_machine_credentials("other", "machine-secret") is None
    assert auth.check_machine_credentials("machine", "machine-secret") == "machine"

    token, issued = auth.issue_session()
    browser_session = auth.resolve_browser_session_from_headers(
        Headers({"cookie": f"{auth.SESSION_COOKIE_NAME}={token}"})
    )
    assert browser_session.authenticated is True
    assert browser_session.username == "operator"
    assert browser_session.csrf_token == issued.csrf_token

    assert auth._read_cookie(Headers({"cookie": "not a valid cookie;"}), auth.SESSION_COOKIE_NAME) is None
    assert auth._read_cookie(Headers({"cookie": "other=value"}), auth.SESSION_COOKIE_NAME) is None
