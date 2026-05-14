"""Legacy import shim for Phase 1 backend domain-layout refactor.

Real implementation lives at ``app/auth/service.py``. Phase 16 deletes
this shim once every caller migrates.

The shim re-exports both the public API and the private helpers that
existing tests reach into (``_decode_session_payload``, ``_read_cookie``),
plus the module-level ``settings`` reference so
``monkeypatch.setattr(app.services.auth.settings, …)`` keeps working
until tests migrate to the new module path.
"""

from app.auth.service import (
    CSRF_HEADER_NAME,
    MUTATING_METHODS,
    SESSION_COOKIE_NAME,
    RequestAuthResult,
    SessionState,
    _decode_session_payload,
    _read_cookie,
    authenticate_operator,
    check_machine_credentials,
    clear_session_cookie,
    is_auth_enabled,
    issue_session,
    machine_password,
    machine_username,
    operator_password,
    operator_username,
    require_valid_csrf,
    resolve_browser_session_from_headers,
    resolve_browser_session_from_token,
    set_session_cookie,
    validate_process_configuration,
)
from app.core.config import settings

__all__ = [
    "CSRF_HEADER_NAME",
    "MUTATING_METHODS",
    "SESSION_COOKIE_NAME",
    "RequestAuthResult",
    "SessionState",
    "_decode_session_payload",
    "_read_cookie",
    "authenticate_operator",
    "check_machine_credentials",
    "clear_session_cookie",
    "is_auth_enabled",
    "issue_session",
    "machine_password",
    "machine_username",
    "operator_password",
    "operator_username",
    "require_valid_csrf",
    "resolve_browser_session_from_headers",
    "resolve_browser_session_from_token",
    "set_session_cookie",
    "settings",
    "validate_process_configuration",
]
