"""Per-domain BaseSettings env/field-name compat coverage.

Each per-domain ``BaseSettings`` introduced by the backend domain-
layout refactor must accept both env-var lookup (preserving the
existing operations-facing names) and Python-field-name kwargs (so
tests can construct via ``AuthConfig(auth_enabled=True)`` and
``monkeypatch.setattr`` keeps working). The ``populate_by_name=True``
+ per-field ``Field(alias=…)`` pattern locks both forms.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent_comm.config import AgentCommConfig
from app.auth.config import AuthConfig
from app.packs.config import PacksConfig


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


def test_agent_comm_config_accepts_field_name_kwargs() -> None:
    cfg = AgentCommConfig(
        agent_auth_username="agent-user",
        agent_auth_password="agent-secret",
        agent_terminal_token="terminal-token",
        agent_terminal_scheme="wss",
    )
    assert cfg.agent_auth_username == "agent-user"
    assert cfg.agent_auth_password == "agent-secret"
    assert cfg.agent_terminal_token == "terminal-token"
    assert cfg.agent_terminal_scheme == "wss"


def test_agent_comm_config_reads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_AGENT_AUTH_USERNAME", "env-agent")
    monkeypatch.setenv("GRIDFLEET_AGENT_AUTH_PASSWORD", "env-secret")
    monkeypatch.setenv("GRIDFLEET_AGENT_TERMINAL_TOKEN", "env-terminal")
    monkeypatch.setenv("GRIDFLEET_AGENT_TERMINAL_SCHEME", "wss")

    cfg = AgentCommConfig()
    assert cfg.agent_auth_username == "env-agent"
    assert cfg.agent_auth_password == "env-secret"
    assert cfg.agent_terminal_token == "env-terminal"
    assert cfg.agent_terminal_scheme == "wss"


def test_agent_comm_config_accepts_alias_kwargs() -> None:
    cfg = AgentCommConfig(
        GRIDFLEET_AGENT_AUTH_USERNAME="alias-agent",
        GRIDFLEET_AGENT_AUTH_PASSWORD="alias-secret",
        GRIDFLEET_AGENT_TERMINAL_TOKEN="alias-terminal",
        GRIDFLEET_AGENT_TERMINAL_SCHEME="wss",
    )
    assert cfg.agent_auth_username == "alias-agent"
    assert cfg.agent_auth_password == "alias-secret"
    assert cfg.agent_terminal_token == "alias-terminal"
    assert cfg.agent_terminal_scheme == "wss"


def test_agent_comm_config_requires_agent_auth_pair() -> None:
    with pytest.raises(ValueError, match="GRIDFLEET_AGENT_AUTH_USERNAME and GRIDFLEET_AGENT_AUTH_PASSWORD"):
        AgentCommConfig(agent_auth_username="agent-user")


def test_settings_forwards_agent_auth_kwargs_to_agent_settings() -> None:
    from app.agent_comm import agent_settings
    from app.core.config import Settings

    original = agent_settings.model_dump()
    try:
        Settings(agent_auth_username="agent-user", agent_auth_password="agent-secret")
        assert agent_settings.agent_auth_username == "agent-user"
        assert agent_settings.agent_auth_password == "agent-secret"
    finally:
        for key, value in original.items():
            setattr(agent_settings, key, value)


def test_settings_agent_terminal_setter_updates_agent_settings() -> None:
    from app.agent_comm import agent_settings
    from app.core.config import settings

    original = agent_settings.agent_terminal_token
    try:
        settings.agent_terminal_token = "terminal-token"
        assert agent_settings.agent_terminal_token == "terminal-token"
        assert settings.agent_terminal_token == "terminal-token"
    finally:
        settings.agent_terminal_token = original


def test_packs_config_accepts_field_name_kwargs() -> None:
    cfg = PacksConfig(driver_pack_storage_dir="/tmp/field-packs")
    assert cfg.driver_pack_storage_dir == Path("/tmp/field-packs")


def test_packs_config_reads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_DRIVER_PACK_STORAGE_DIR", "/tmp/env-packs")

    cfg = PacksConfig()
    assert cfg.driver_pack_storage_dir == Path("/tmp/env-packs")


def test_packs_config_accepts_alias_kwargs() -> None:
    cfg = PacksConfig(GRIDFLEET_DRIVER_PACK_STORAGE_DIR="/tmp/alias-packs")
    assert cfg.driver_pack_storage_dir == Path("/tmp/alias-packs")


def test_settings_forwards_pack_storage_kwargs_to_packs_settings() -> None:
    from app.core.config import Settings
    from app.packs import packs_settings

    original = packs_settings.model_dump()
    try:
        settings = Settings(driver_pack_storage_dir=Path("/tmp/settings-packs"))
        assert packs_settings.driver_pack_storage_dir == Path("/tmp/settings-packs")
        assert settings.driver_pack_storage_dir == Path("/tmp/settings-packs")
    finally:
        for key, value in original.items():
            setattr(packs_settings, key, value)


def test_settings_pack_storage_setter_updates_packs_settings() -> None:
    from app.core.config import settings
    from app.packs import packs_settings

    original = packs_settings.driver_pack_storage_dir
    try:
        settings.driver_pack_storage_dir = Path("/tmp/setter-packs")
        assert packs_settings.driver_pack_storage_dir == Path("/tmp/setter-packs")
        assert settings.driver_pack_storage_dir == Path("/tmp/setter-packs")
    finally:
        settings.driver_pack_storage_dir = original
