import base64
from collections.abc import Iterator

import jwt as _pyjwt
import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from starlette.datastructures import Headers

from app.auth import auth_settings as settings
from app.auth import service as auth
from app.auth.config import AuthConfig

HOST_PAYLOAD = {
    "hostname": "auth-host-01",
    "ip": "192.168.1.200",
    "os_type": "linux",
    "agent_port": 5100,
}


def _basic_auth_header(username: str, password: str) -> dict[str, str]:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


@pytest.fixture
def auth_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    values = {
        "auth_username": "operator",
        "auth_password": "operator-secret",
        "auth_session_secret": "session-secret-for-tests-pad-to-32-bytes",
        "machine_auth_username": "machine",
        "machine_auth_password": "machine-secret",
    }
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_username", values["auth_username"])
    monkeypatch.setattr(settings, "auth_password", values["auth_password"])
    monkeypatch.setattr(settings, "auth_session_secret", values["auth_session_secret"])
    monkeypatch.setattr(settings, "auth_session_ttl_sec", 28_800)
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "machine_auth_username", values["machine_auth_username"])
    monkeypatch.setattr(settings, "machine_auth_password", values["machine_auth_password"])
    yield values


def test_settings_require_auth_configuration_when_enabled() -> None:
    with pytest.raises(ValidationError, match="Auth is enabled but required settings are missing"):
        AuthConfig(auth_enabled=True)


async def test_auth_session_reports_disabled_when_feature_off(client: AsyncClient) -> None:
    response = await client.get("/api/auth/session")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "authenticated": False,
        "username": None,
        "csrf_token": None,
        "expires_at": None,
    }


async def test_login_and_logout_report_disabled_when_auth_off(client: AsyncClient) -> None:
    login_response = await client.post("/api/auth/login", json={"username": "operator", "password": "secret"})
    logout_response = await client.post("/api/auth/logout")

    expected = {
        "enabled": False,
        "authenticated": False,
        "username": None,
        "csrf_token": None,
        "expires_at": None,
    }
    assert login_response.status_code == 200
    assert login_response.json() == expected
    assert logout_response.status_code == 200
    assert logout_response.json() == expected


async def test_login_sets_session_cookie_and_restores_session(
    client: AsyncClient,
    auth_settings: dict[str, str],
) -> None:
    response = await client.post(
        "/api/auth/login",
        json={
            "username": auth_settings["auth_username"],
            "password": auth_settings["auth_password"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["authenticated"] is True
    assert body["username"] == auth_settings["auth_username"]
    assert isinstance(body["csrf_token"], str) and body["csrf_token"]
    assert body["expires_at"] is not None
    assert client.cookies.get("gridfleet_session")

    session_response = await client.get("/api/auth/session")
    assert session_response.status_code == 200
    assert session_response.json()["authenticated"] is True

    protected_response = await client.get("/api/hosts")
    assert protected_response.status_code == 200


async def test_login_rejects_invalid_credentials(client: AsyncClient, auth_settings: dict[str, str]) -> None:
    response = await client.post(
        "/api/auth/login",
        json={"username": auth_settings["auth_username"], "password": "wrong"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_protected_routes_require_machine_or_browser_auth(
    client: AsyncClient,
    auth_settings: dict[str, str],
) -> None:
    unauthorized = await client.get("/api/hosts")
    assert unauthorized.status_code == 401

    machine_response = await client.get(
        "/api/hosts",
        headers=_basic_auth_header(
            auth_settings["machine_auth_username"],
            auth_settings["machine_auth_password"],
        ),
    )
    assert machine_response.status_code == 200


async def test_health_endpoints_stay_open_but_metrics_is_protected(
    client: AsyncClient,
    auth_settings: dict[str, str],
) -> None:
    live = await client.get("/health/live")
    ready = await client.get("/api/health")

    assert live.status_code == 200
    assert ready.status_code in {200, 503}

    metrics_unauth = await client.get("/metrics")
    assert metrics_unauth.status_code == 401

    metrics_auth = await client.get(
        "/metrics",
        headers=_basic_auth_header(
            auth_settings["machine_auth_username"],
            auth_settings["machine_auth_password"],
        ),
    )
    assert metrics_auth.status_code == 200


async def test_execution_plane_paths_require_auth(
    client: AsyncClient,
    auth_settings: dict[str, str],
) -> None:
    runs_list = await client.get("/api/runs")
    assert runs_list.status_code == 401

    sessions_list = await client.get("/api/sessions")
    assert sessions_list.status_code == 401

    catalog = await client.get("/api/driver-packs/catalog")
    assert catalog.status_code == 401


async def test_execution_plane_paths_accept_machine_auth(
    client: AsyncClient,
    auth_settings: dict[str, str],
) -> None:
    machine_headers = _basic_auth_header(
        auth_settings["machine_auth_username"],
        auth_settings["machine_auth_password"],
    )

    runs_list = await client.get("/api/runs", headers=machine_headers)
    assert runs_list.status_code == 200

    sessions_list = await client.get("/api/sessions", headers=machine_headers)
    assert sessions_list.status_code == 200

    catalog = await client.get("/api/driver-packs/catalog", headers=machine_headers)
    assert catalog.status_code == 200


async def test_browser_mutations_require_csrf(
    client: AsyncClient,
    auth_settings: dict[str, str],
) -> None:
    login_response = await client.post(
        "/api/auth/login",
        json={
            "username": auth_settings["auth_username"],
            "password": auth_settings["auth_password"],
        },
    )
    csrf_token = login_response.json()["csrf_token"]

    missing_csrf = await client.post("/api/hosts", json=HOST_PAYLOAD)
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "FORBIDDEN"

    with_csrf = await client.post(
        "/api/hosts",
        json=HOST_PAYLOAD,
        headers={"X-CSRF-Token": csrf_token},
    )
    assert with_csrf.status_code == 201


async def test_machine_auth_bypasses_csrf(client: AsyncClient, auth_settings: dict[str, str]) -> None:
    response = await client.post(
        "/api/hosts",
        json=HOST_PAYLOAD,
        headers=_basic_auth_header(
            auth_settings["machine_auth_username"],
            auth_settings["machine_auth_password"],
        ),
    )

    assert response.status_code == 201


async def test_logout_clears_session(
    client: AsyncClient,
    auth_settings: dict[str, str],
) -> None:
    await client.post(
        "/api/auth/login",
        json={
            "username": auth_settings["auth_username"],
            "password": auth_settings["auth_password"],
        },
    )

    # auth.router is unprotected (login/logout/session must work without an existing
    # valid session), so logout no longer enforces CSRF. It clears the cookie regardless.
    response = await client.post("/api/auth/logout")
    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "authenticated": False,
        "username": None,
        "csrf_token": None,
        "expires_at": None,
    }

    session_response = await client.get("/api/auth/session")
    assert session_response.json()["authenticated"] is False


async def test_agent_registration_requires_machine_auth(
    client: AsyncClient,
    auth_settings: dict[str, str],
) -> None:
    register_payload = {**HOST_PAYLOAD, "capabilities": {"orchestration_contract_version": 2}}
    unauthorized = await client.post("/api/hosts/register", json=register_payload)
    assert unauthorized.status_code == 401

    authorized = await client.post(
        "/api/hosts/register",
        json=register_payload,
        headers=_basic_auth_header(
            auth_settings["machine_auth_username"],
            auth_settings["machine_auth_password"],
        ),
    )
    assert authorized.status_code == 201


async def test_agent_driver_pack_state_routes_require_machine_auth(
    client: AsyncClient,
    auth_settings: dict[str, str],
) -> None:
    host_id = "00000000-0000-0000-0000-000000000001"

    desired_unauthorized = await client.get("/agent/driver-packs/desired", params={"host_id": host_id})
    assert desired_unauthorized.status_code == 401

    status_unauthorized = await client.post(
        "/agent/driver-packs/status",
        json={"host_id": host_id, "runtimes": [], "packs": [], "doctor": []},
    )
    assert status_unauthorized.status_code == 401

    machine_headers = _basic_auth_header(
        auth_settings["machine_auth_username"],
        auth_settings["machine_auth_password"],
    )
    desired_authorized = await client.get(
        "/agent/driver-packs/desired",
        params={"host_id": host_id},
        headers=machine_headers,
    )
    assert desired_authorized.status_code == 200

    status_authorized = await client.post(
        "/agent/driver-packs/status",
        json={"host_id": host_id, "runtimes": [], "packs": [], "doctor": []},
        headers=machine_headers,
    )
    assert status_authorized.status_code == 204


async def test_event_stream_path_requires_auth(
    client: AsyncClient,
    auth_settings: dict[str, str],
) -> None:
    # Auth is now enforced by require_any_auth dep on the events router, not by middleware.
    # We only assert the rejection path here; the happy-path SSE stream is exercised
    # by tests for the events router itself. Asserting status_code under .stream() still
    # blocks until the server sends headers, which an SSE handler may defer until the
    # first event — that's a different concern from auth and is out of scope for this file.
    unauthorized = await client.get("/api/events")
    assert unauthorized.status_code == 401


async def test_rotating_session_secret_invalidates_existing_session(
    client: AsyncClient,
    auth_settings: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_response = await client.post(
        "/api/auth/login",
        json={
            "username": auth_settings["auth_username"],
            "password": auth_settings["auth_password"],
        },
    )
    assert login_response.status_code == 200

    monkeypatch.setattr(settings, "auth_session_secret", "rotated-session-secret-padded-32-bytes")

    protected_response = await client.get("/api/hosts")
    assert protected_response.status_code == 401

    session_response = await client.get("/api/auth/session")
    assert session_response.status_code == 200
    assert session_response.json() == {
        "enabled": True,
        "authenticated": False,
        "username": None,
        "csrf_token": None,
        "expires_at": None,
    }


async def test_operator_password_rotation_keeps_stateless_session_until_secret_rotates(
    client: AsyncClient,
    auth_settings: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_response = await client.post(
        "/api/auth/login",
        json={
            "username": auth_settings["auth_username"],
            "password": auth_settings["auth_password"],
        },
    )
    assert login_response.status_code == 200

    monkeypatch.setattr(settings, "auth_password", "rotated-operator-secret")

    protected_response = await client.get("/api/hosts")
    assert protected_response.status_code == 200

    session_response = await client.get("/api/auth/session")
    assert session_response.status_code == 200
    assert session_response.json()["authenticated"] is True


def test_issue_session_uses_configured_username(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth.settings, "auth_username", "configured-operator")
    monkeypatch.setattr(auth.settings, "auth_password", "configured-password")
    monkeypatch.setattr(auth.settings, "auth_session_secret", "session-secret-padded-to-32-bytes-min")

    token, session = auth.issue_session()
    payload = auth._decode_session_payload(token)

    assert session.username == "configured-operator"
    assert payload is not None
    assert payload["sub"] == "configured-operator"
    assert "fp" not in payload
    assert "configured-password" not in token


def test_issue_session_does_not_read_operator_password(monkeypatch: pytest.MonkeyPatch) -> None:
    class PasswordRaisingSettings:
        auth_username = "operator"
        auth_session_secret = "session-secret-padded-to-32-bytes-min"
        auth_session_ttl_sec = 28_800

        @property
        def auth_password(self) -> str:
            raise AssertionError("session issuance must not read the operator password")

    monkeypatch.setattr(auth, "settings", PasswordRaisingSettings())

    token, session = auth.issue_session()

    assert token
    assert session.authenticated is True
    assert session.username == "operator"


def test_issue_session_does_not_store_password_derived_marker_in_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth.settings, "auth_enabled", True)
    monkeypatch.setattr(auth.settings, "auth_username", "operator")
    monkeypatch.setattr(auth.settings, "auth_password", "old-password")
    monkeypatch.setattr(auth.settings, "auth_session_secret", "session-secret-padded-to-32-bytes-min")
    monkeypatch.setattr(auth.settings, "auth_session_ttl_sec", 28_800)

    token, _ = auth.issue_session()
    payload = auth._decode_session_payload(token)

    assert payload is not None
    assert "cv" not in payload
    assert "old-password" not in token

    monkeypatch.setattr(auth.settings, "auth_password", "new-password")

    session = auth.resolve_browser_session_from_headers(Headers({"cookie": f"{auth.SESSION_COOKIE_NAME}={token}"}))

    assert session.authenticated is True
    assert session.username == "operator"


def test_issue_session_token_is_jwt_hs256(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_username", "alice", raising=False)
    monkeypatch.setattr(settings, "auth_session_secret", "test-secret-padded-to-32-bytes-min", raising=False)
    monkeypatch.setattr(settings, "auth_session_ttl_sec", 60, raising=False)

    token, session = auth.issue_session()

    payload = _pyjwt.decode(token, "test-secret-padded-to-32-bytes-min", algorithms=["HS256"])
    assert payload["sub"] == "alice"
    assert payload["csrf"] == session.csrf_token
    assert payload["exp"] > payload["iat"]


def test_decode_session_payload_rejects_tampered_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_username", "alice", raising=False)
    monkeypatch.setattr(settings, "auth_session_secret", "test-secret-padded-to-32-bytes-min", raising=False)
    monkeypatch.setattr(settings, "auth_session_ttl_sec", 60, raising=False)

    token, _ = auth.issue_session()
    head, body, sig = token.split(".")
    forged = ".".join([head, body, sig[:-2] + "AA"])
    assert auth._decode_session_payload(forged) is None


def test_decode_session_payload_rejects_alg_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_username", "alice", raising=False)
    monkeypatch.setattr(settings, "auth_session_secret", "test-secret-padded-to-32-bytes-min", raising=False)

    forged = _pyjwt.encode({"sub": "alice", "csrf": "x", "exp": 9999999999}, key="", algorithm="none")
    assert auth._decode_session_payload(forged) is None


def test_decode_session_payload_rejects_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_username", "alice", raising=False)
    monkeypatch.setattr(settings, "auth_session_secret", "test-secret-padded-to-32-bytes-min", raising=False)

    expired = _pyjwt.encode(
        {"sub": "alice", "csrf": "x", "iat": 0, "exp": 1},
        "test-secret-padded-to-32-bytes-min",
        algorithm="HS256",
    )
    assert auth._decode_session_payload(expired) is None


def test_decode_session_payload_rejects_missing_required_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_username", "alice", raising=False)
    monkeypatch.setattr(settings, "auth_session_secret", "test-secret-padded-to-32-bytes-min", raising=False)

    no_csrf = _pyjwt.encode(
        {"sub": "alice", "exp": 9999999999}, "test-secret-padded-to-32-bytes-min", algorithm="HS256"
    )
    assert auth._decode_session_payload(no_csrf) is None


def test_decode_session_payload_returns_none_on_missing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_username", "alice", raising=False)
    monkeypatch.setattr(settings, "auth_session_secret", None, raising=False)
    # Any non-empty string passes the format gate but must surface None, not raise.
    assert auth._decode_session_payload("a.b.c") is None


def test_auth_token_and_cookie_guard_branches(auth_settings: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    secret = auth_settings["auth_session_secret"]
    monkeypatch.setattr(auth.settings, "auth_enabled", True)
    monkeypatch.setattr(auth.settings, "auth_username", auth_settings["auth_username"])
    monkeypatch.setattr(auth.settings, "auth_session_secret", secret)

    def token_for(payload: dict[str, object]) -> str:
        return _pyjwt.encode(payload, secret, algorithm="HS256")

    # Non-JWT and malformed tokens are rejected
    assert auth._decode_session_payload("not-a-jwt") is None
    assert auth._decode_session_payload("bad.token.here") is None
    # Tampered signature rejected
    real_token, _ = auth.issue_session()
    head, body, sig = real_token.split(".")
    assert auth._decode_session_payload(".".join([head, body, sig[:-2] + "AA"])) is None

    token, _ = auth.issue_session()
    payload = auth._decode_session_payload(token)
    assert payload is not None

    # Missing required claim: jwt.decode rejects it → returns None
    missing_sub = dict(payload)
    missing_sub.pop("sub")
    assert auth._decode_session_payload(token_for(missing_sub)) is None
    session = auth.resolve_browser_session_from_headers(
        Headers({"cookie": f"{auth.SESSION_COOKIE_NAME}={token_for(missing_sub)}"})
    )
    assert session.authenticated is False

    empty_user = {**payload, "sub": ""}
    assert (
        auth.resolve_browser_session_from_headers(
            Headers({"cookie": f"{auth.SESSION_COOKIE_NAME}={token_for(empty_user)}"})
        ).authenticated
        is False
    )
    empty_csrf = {**payload, "csrf": ""}
    assert (
        auth.resolve_browser_session_from_headers(
            Headers({"cookie": f"{auth.SESSION_COOKIE_NAME}={token_for(empty_csrf)}"})
        ).authenticated
        is False
    )

    def _bad_cookie_parser(_raw: str) -> dict[str, str]:
        raise ValueError("bad cookie")

    monkeypatch.setattr(auth, "cookie_parser", _bad_cookie_parser)
    assert auth._read_cookie(Headers({"cookie": "broken=cookie"}), auth.SESSION_COOKIE_NAME) is None


def test_check_machine_credentials_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "machine_auth_username", "bot", raising=False)
    monkeypatch.setattr(settings, "machine_auth_password", "shh", raising=False)
    assert auth.check_machine_credentials("bot", "shh") == "bot"


def test_check_machine_credentials_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "machine_auth_username", "bot", raising=False)
    monkeypatch.setattr(settings, "machine_auth_password", "shh", raising=False)
    assert auth.check_machine_credentials("bot", "wrong") is None
    assert auth.check_machine_credentials("other", "shh") is None


def test_resolve_browser_session_from_token_none_returns_unauthenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_username", "alice", raising=False)
    monkeypatch.setattr(settings, "auth_session_secret", "test-secret-padded-to-32-bytes-min", raising=False)
    state = auth.resolve_browser_session_from_token(None)
    assert state.enabled is True
    assert state.authenticated is False


def test_resolve_browser_session_from_token_valid_returns_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_username", "alice", raising=False)
    monkeypatch.setattr(settings, "auth_session_secret", "test-secret-padded-to-32-bytes-min", raising=False)
    monkeypatch.setattr(settings, "auth_session_ttl_sec", 60, raising=False)
    token, _ = auth.issue_session()
    state = auth.resolve_browser_session_from_token(token)
    assert state.authenticated is True
    assert state.username == "alice"


def test_resolve_browser_session_from_token_wrong_username_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_username", "alice", raising=False)
    monkeypatch.setattr(settings, "auth_session_secret", "test-secret-padded-to-32-bytes-min", raising=False)
    monkeypatch.setattr(settings, "auth_session_ttl_sec", 60, raising=False)
    token, _ = auth.issue_session()
    # Rename operator mid-session.
    monkeypatch.setattr(settings, "auth_username", "bob", raising=False)
    state = auth.resolve_browser_session_from_token(token)
    assert state.authenticated is False
