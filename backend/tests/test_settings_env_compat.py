"""Per-domain BaseSettings env/field-name compat coverage.

Each per-domain ``BaseSettings`` introduced by the backend domain-
layout refactor must accept both env-var lookup (preserving the
existing operations-facing names) and Python-field-name kwargs (so
tests can construct via ``AuthConfig(auth_enabled=True)`` and
``monkeypatch.setattr`` keeps working). The ``populate_by_name=True``
+ per-field ``Field(alias=…)`` pattern locks both forms.
"""

from __future__ import annotations

import pytest

from app.auth.config import AuthConfig


def test_auth_config_accepts_field_name_kwargs() -> None:
    cfg = AuthConfig(
        auth_enabled=True,
        auth_username="operator",
        auth_password="operator-secret",
        auth_session_secret="session-secret",
        machine_auth_username="machine",
        machine_auth_password="machine-secret",
    )
    assert cfg.auth_enabled is True
    assert cfg.auth_username == "operator"
    assert cfg.machine_auth_username == "machine"


def test_auth_config_reads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_AUTH_ENABLED", "true")
    monkeypatch.setenv("GRIDFLEET_AUTH_USERNAME", "env-operator")
    monkeypatch.setenv("GRIDFLEET_AUTH_PASSWORD", "env-password")
    monkeypatch.setenv("GRIDFLEET_AUTH_SESSION_SECRET", "env-secret")
    monkeypatch.setenv("GRIDFLEET_MACHINE_AUTH_USERNAME", "env-machine")
    monkeypatch.setenv("GRIDFLEET_MACHINE_AUTH_PASSWORD", "env-machine-pass")

    cfg = AuthConfig()
    assert cfg.auth_enabled is True
    assert cfg.auth_username == "env-operator"
    assert cfg.machine_auth_username == "env-machine"


def test_auth_config_accepts_alias_kwargs() -> None:
    cfg = AuthConfig(
        GRIDFLEET_AUTH_ENABLED=True,
        GRIDFLEET_AUTH_USERNAME="alias-operator",
        GRIDFLEET_AUTH_PASSWORD="alias-password",
        GRIDFLEET_AUTH_SESSION_SECRET="alias-secret",
        GRIDFLEET_MACHINE_AUTH_USERNAME="alias-machine",
        GRIDFLEET_MACHINE_AUTH_PASSWORD="alias-machine-pass",
    )
    assert cfg.auth_enabled is True
    assert cfg.auth_username == "alias-operator"


def test_auth_config_missing_required_fields_when_enabled() -> None:
    with pytest.raises(ValueError, match="Auth is enabled but required settings are missing"):
        AuthConfig(auth_enabled=True)


def test_auth_config_session_ttl_must_be_positive() -> None:
    with pytest.raises(ValueError, match="GRIDFLEET_AUTH_SESSION_TTL_SEC must be at least 1 second"):
        AuthConfig(auth_session_ttl_sec=0)


def test_settings_forwards_auth_enabled_to_auth_settings() -> None:
    from app.auth import auth_settings
    from app.core.config import settings

    original = auth_settings.auth_enabled
    try:
        settings.auth_enabled = True
        assert auth_settings.auth_enabled is True
        assert settings.auth_enabled is True
    finally:
        settings.auth_enabled = original
